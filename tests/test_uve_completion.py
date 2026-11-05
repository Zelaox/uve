from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agent_training.manifest_loader import load_json_ref, pipeline_root, ManifestError
from agent_training.uve.bundle import load_manifest_bundle
from agent_training.uve.orchestrator import run_probe
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.lease_policy import acquire_probe_lease, release_probe_lease
from agent_training.uve.leases import LeaseManager
from agent_training.uve.export_training import build_export_row, derive_training_label

SMOKE = "config/uve/profiles/emulator_workstation_smoke.json"


def write_json(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def local_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("UVE_TEST_ROOT", str(tmp_path))
    root_ref = write_json(tmp_path / "root.json", {"root_env": "UVE_TEST_ROOT"})
    profile = {"project_id": "test", "environment": "test", "project_root_ref": root_ref,
               "identity": {"watch_globs": ["*.txt"], "ignore_globs": []},
               "execution_binding_profile_id": "emulator_workstation_smoke"}
    ref = write_json(tmp_path / "profile.json", profile)
    (tmp_path / "source.txt").write_text("before", encoding="utf-8")
    return ref, profile


def gate(probes, **kwargs):
    return evaluate_gate("smoke_close_issue", probes,
                         manifest_bundle_sha256=load_manifest_bundle()["manifest_bundle_sha256"],
                         gate_evaluator_ref="independent", **kwargs)


def test_mid_probe_edit_cannot_approve(tmp_path, monkeypatch):
    ref, profile = local_profile(tmp_path, monkeypatch)
    def mutate(*args):
        (tmp_path / "source.txt").write_text("after")
        return {"verdict": "passed"}
    monkeypatch.setattr("agent_training.uve.orchestrator._invoke_adapter", mutate)
    receipt = run_probe(ref, "dry_echo")
    assert receipt["verdict"] == "not_verified"
    assert receipt["toctou"]["identity_stable"] is False
    assert gate([receipt])["decision"] != "approved"


def test_profile_target_tampering_is_rejected(tmp_path, monkeypatch):
    ref, profile = local_profile(tmp_path, monkeypatch)
    receipt = run_probe(ref, "dry_echo")
    profile["environment"] = "changed"
    write_json(Path(ref), profile)
    assert gate([receipt])["rejection_basis"] == "manifest_mismatch"


def test_adapter_exception_produces_receipt_and_releases_lease(tmp_path, monkeypatch):
    ref, profile = local_profile(tmp_path, monkeypatch)
    profile["lease_requirements"] = {"dry_echo": {"resource_kind": "mutation_work_dir", "key_values": {"work_dir": str(tmp_path)}}}
    write_json(Path(ref), profile)
    def crash(*args):
        raise RuntimeError("adapter broke")
    monkeypatch.setattr("agent_training.uve.orchestrator._invoke_adapter", crash)
    receipt = run_probe(ref, "dry_echo")
    assert receipt["verdict"] == "not_verified"
    token, denied = acquire_probe_lease(profile, "dry_echo", "next")
    assert token and denied is None
    release_probe_lease(profile, "dry_echo", token)


def test_same_role_and_other_process_cannot_share_lease(tmp_path):
    manager = LeaseManager()
    key = str(tmp_path)
    token = manager.acquire("mutation_work_dir", key, "uve_runner")
    try:
        assert manager.acquire("mutation_work_dir", key, "uve_runner") is None
        script = ("from agent_training.uve.leases import LeaseManager; "
                  "import sys; sys.exit(0 if LeaseManager().acquire('mutation_work_dir', sys.argv[1], 'uve_runner') is None else 1)")
        process = subprocess.run([sys.executable, "-c", script, key], cwd=pipeline_root(), capture_output=True)
        assert process.returncode == 0, process.stderr
    finally:
        manager.release("mutation_work_dir", key, token)


def test_namespace_collision_and_partial_acquisition_rollback(tmp_path):
    requirement = [{"resource_kind": "test_database", "key_values": {"host": "local", "port": 1234, "namespace": str(tmp_path)}},
                   {"resource_kind": "mutation_work_dir", "key_values": {"work_dir": str(tmp_path)}}]
    a = {"project_id": "a", "lease_requirements": {"probe": requirement}}
    b = {"project_id": "b", "lease_requirements": {"probe": requirement}}
    token, denied = acquire_probe_lease(a, "probe", "same-role")
    try:
        other, denied = acquire_probe_lease(b, "probe", "same-role")
        assert other is None and denied["failure_class"] == "lease_denied"
    finally:
        release_probe_lease(a, "probe", token)
    other, denied = acquire_probe_lease(b, "probe", "same-role")
    assert other and denied is None
    release_probe_lease(b, "probe", other)


def test_snapshot_uses_copied_bytes(tmp_path, monkeypatch):
    ref, profile = local_profile(tmp_path, monkeypatch)
    original = load_json_ref
    def binding(ref):
        doc = original(ref)
        if ref.endswith("uve_execution_binding_manifest.json"):
            doc["profiles"]["emulator_workstation_smoke"]["snapshot_mode"] = "required"
        return doc
    original_bundle = load_manifest_bundle
    def snapshot_bundle():
        doc = original_bundle()
        doc["_loaded"]["config/uve/uve_execution_binding_manifest.json"]["profiles"]["emulator_workstation_smoke"]["snapshot_mode"] = "required"
        return doc
    monkeypatch.setattr("agent_training.uve.orchestrator.load_manifest_bundle", snapshot_bundle)
    def inspect(adapter, probe, ctx):
        snapshot = ctx["workspace_root"]
        assert snapshot != tmp_path
        assert (snapshot / "source.txt").read_text() == "before"
        (tmp_path / "source.txt").write_text("after")
        assert (snapshot / "source.txt").read_text() == "before"
        return {"verdict": "passed"}
    monkeypatch.setattr("agent_training.uve.orchestrator._invoke_adapter", inspect)
    receipt = run_probe(ref, "dry_echo")
    assert receipt["toctou"]["snapshot_binding_sha256"]
    assert receipt["verdict"] == "not_verified"


def test_ui_interaction_success_state_failure(monkeypatch):
    from agent_training.uve.adapters.adb import _run_ui_flow
    flow = {"steps": [{"step_id": "tap", "kind": "tap_element", "selector": {"text": "Save"}},
                      {"step_id": "state", "kind": "shell_property", "property": "app.saved", "expected": "1"}]}
    monkeypatch.setattr("agent_training.uve.adapters.adb.load_json_ref", lambda ref: flow)
    def adb(spec, args, timeout):
        if "uiautomator" in args:
            body = '<hierarchy><node text="Save" bounds="[0,0][100,100]"/></hierarchy>'
        elif "getprop" in args:
            body = "0"
        else:
            body = ""
        return subprocess.CompletedProcess(args, 0, body, "")
    monkeypatch.setattr("agent_training.uve.adapters.adb._adb_run", adb)
    result = _run_ui_flow({"timeout_seconds": 1}, "unused")
    assert result["verdict"] == "failed"
    assert result["ui_steps"][0]["ok"] is True
    assert result["failed_step"] == "state"


def test_export_hash_tampering_and_metadata_override():
    receipt = run_probe(SMOKE, "dry_echo")
    approved = gate([receipt])
    tampered = deepcopy(receipt)
    tampered["observation"]["forged"] = True
    with pytest.raises(ManifestError, match="content hash"):
        build_export_row(failure_class="assertion_mismatch", gate_receipt=approved,
                         probe_receipts=[tampered], required_probe_ids=["dry_echo"], fields={})
    with pytest.raises(ManifestError, match="override"):
        build_export_row(failure_class="assertion_mismatch", gate_receipt=approved,
                         probe_receipts=[receipt], required_probe_ids=["dry_echo"], fields={"training_label": "verified_success"})
    with pytest.raises(ManifestError):
        derive_training_label(approved, [], ["dry_echo"])


def test_raw_log_preserves_stack_export_only_masks():
    from agent_training.uve.evidence_store import EvidenceStore
    from agent_training.uve.export_training import apply_secret_placeholders
    raw = "TrainingPlanner.tsx:184\nTypeError: undefined\n  at save (src/form.ts:9)\nAuthorization: Bearer token123"
    store = EvidenceStore()
    import uuid
    name = f"completion/{uuid.uuid4().hex}.log"
    store.write_raw("uve_runner", name, raw)
    exported = apply_secret_placeholders(raw, "config/uve/uve_secret_placeholder_manifest.json")
    assert store.read_raw("uve_gate", name) == raw
    assert "TrainingPlanner.tsx:184" in exported and "src/form.ts:9" in exported
    assert "token123" not in exported
    with pytest.raises(ManifestError):
        store.read_raw("repair_agent", name)
    with pytest.raises(ManifestError):
        store.write_raw("uve_runner", "../escape.log", "no")


def test_real_code_failure_repair_export(tmp_path, monkeypatch):
    ref, profile = local_profile(tmp_path, monkeypatch)
    profile["identity"]["watch_globs"] = ["app.py"]
    command = {"argv": ["{python}", "app.py"], "timeout_seconds": 10,
               "expected_returncode": 0, "expected_stdout": "ready"}
    profile["command_probe_refs"] = {"command_check": write_json(tmp_path / "command.json", command)}
    write_json(Path(ref), profile)
    source = tmp_path / "app.py"
    source.write_text("raise RuntimeError('synthetic fault')")
    failed = run_probe(ref, "command_check")
    assert failed["verdict"] == "failed" and "RuntimeError" in failed["observation"]["stderr"]
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    failure_gate = evaluate_gate("command_check", [failed], manifest_bundle_sha256=bundle, gate_evaluator_ref="test-witness")
    row = build_export_row(failure_class="assertion_mismatch", gate_receipt=failure_gate,
                           probe_receipts=[failed], required_probe_ids=["command_check"], fields={"source_line": 1})
    assert row["training_label"] == "verified_failure"
    source.write_text("print('ready')")
    passed = run_probe(ref, "command_check")
    approval = evaluate_gate("command_check", [passed], manifest_bundle_sha256=bundle, gate_evaluator_ref="test-witness")
    row = build_export_row(failure_class="assertion_mismatch", gate_receipt=approval,
                           probe_receipts=[passed], required_probe_ids=["command_check"], fields={"source_line": 1})
    assert row["training_label"] == "verified_success"
    assert failed["subject_identity"] != passed["subject_identity"]


def test_live_protocol_recovery_kill_restart_retest(tmp_path, monkeypatch):
    import socket
    from agent_training.uve import recovery
    ref, profile = local_profile(tmp_path, monkeypatch)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = tmp_path / "server.py"
    server.write_text("from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                      "class Handler(BaseHTTPRequestHandler):\n"
                      " def do_GET(self):\n"
                      "  self.send_response(200); self.end_headers(); self.wfile.write(b'packager-status:running')\n"
                      f"HTTPServer(('127.0.0.1',{port}),Handler).serve_forever()\n")
    profile["metro_status_ref"] = write_json(tmp_path / "metro.json", {
        "status_url": f"http://127.0.0.1:{port}/status", "required_body_substring": "packager-status:running", "timeout_seconds": 1})
    profile["recovery"] = {"automatic_restart": True}
    profile["recovery_action_refs"] = {"restart_metro": write_json(tmp_path / "restart.json", {
        "start_argv": [sys.executable, str(server)], "stop_timeout_seconds": 5,
        "retest_attempts": 10, "retest_interval_seconds": 0.1, "log_filename": "test-recovery.log"})}
    resource = {"resource_kind": "dev_server_port", "key_values": {"host": "127.0.0.1", "port": port}}
    profile["lease_requirements"] = {"restart_metro": resource, "metro_status": resource}
    write_json(Path(ref), profile)
    original_load = recovery.load_json_ref
    def policy(reference):
        doc = original_load(reference)
        if reference.endswith("uve_recovery_manifest.json"):
            doc["profiles"]["test"] = {"allowed_actions": ["restart_metro"]}
        return doc
    monkeypatch.setattr(recovery, "load_json_ref", policy)
    assert run_probe(ref, "metro_status")["verdict"] == "failed"
    owner = f"{tmp_path.resolve()}:restart_metro"
    try:
        first = recovery.run_recovery_drill(ref, "restart_metro")
        assert first["status"] == "complete", first
        process = recovery._owned[owner]
        process.kill()
        process.wait(timeout=5)
        assert run_probe(ref, "metro_status")["verdict"] == "failed"
        second = recovery.run_recovery_drill(ref, "restart_metro")
        assert second["status"] == "complete", second
        assert second["reverification"][-1]["verdict"] == "passed"
        assert recovery._owned[owner].pid != process.pid
    finally:
        process = recovery._owned.pop(owner, None)
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_live_ai_lab_runner_invokes_authoritative_api(tmp_path, monkeypatch):
    import types
    from agent_training.uve.ai_lab_runner import run_training_quality
    lab = tmp_path / "lab"
    lab.mkdir()
    monkeypatch.setenv("UVE_AI_LAB_ROOT", str(lab))
    write_json(lab / "state.json", {"optimizer_step": 1})
    write_json(lab / "runtime.json", {"training": {"training_stage": "0"}})
    spec = {"run_path_relative": "run", "checkpoint_relative": "run/checkpoints/step-1",
            "training_state_relative": "state.json", "runtime_contract_relative": "runtime.json",
            "training_stage": "0", "tiers": ["V1"], "receipt_scope": "online_quality"}
    reference = write_json(tmp_path / "run.json", spec)
    seen = {}
    def runner(**kwargs):
        seen.update(kwargs)
        return {"tier_results": {"V1": {"checks": []}}}
    module = types.SimpleNamespace(__file__=str(lab / "runner.py"), run_quality_tiers=runner)
    monkeypatch.setattr("agent_training.uve.ai_lab_runner.importlib.import_module", lambda name: module)
    result = run_training_quality({"ai_lab_run_ref": reference})
    assert "V1" in result["tier_results"]
    assert seen["checkpoint_dir"] == lab / "run/checkpoints/step-1"
    assert seen["training_state"]["optimizer_step"] == 1


def test_witness_rejects_candidate_regression_preserves_approval(tmp_path):
    import shutil
    from agent_training.uve.witness import run_witness_bundle
    from agent_training.uve.bundle import resolve_log_dir
    good = run_witness_bundle()
    assert good["approved"], good
    approved_path = resolve_log_dir() / "witness/last_approved.json"
    previous = approved_path.read_bytes()
    candidate = tmp_path / "candidate"
    for directory in ("agent_training", "config"):
        shutil.copytree(pipeline_root()/directory, candidate/directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (candidate / "agent_training/uve/adapters/dry.py").write_text(
        "def run_probe(**kwargs):\n return {'verdict': 'passed'}\n")
    result = run_witness_bundle(candidate)
    assert result["gate"]["decision"] == "rejected"
    assert result["gate"]["rejection_basis"] == "engine_fault"
    assert approved_path.read_bytes() == previous


def test_corrupt_manifest_cannot_approve(monkeypatch):
    receipt = run_probe(SMOKE, "dry_echo")
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    def broken():
        raise ValueError("corrupt manifest")
    monkeypatch.setattr("agent_training.uve.bundle.load_manifest_bundle", broken)
    result = evaluate_gate("smoke_close_issue", [receipt], manifest_bundle_sha256=bundle, gate_evaluator_ref="independent")
    assert result["decision"] == "not_verified"
    assert result["rejection_basis"] == "engine_fault"


def test_stale_source_and_self_authored_repair_cannot_close(tmp_path, monkeypatch):
    ref, profile = local_profile(tmp_path, monkeypatch)
    receipt = run_probe(ref, "dry_echo", repair_actor_refs=["repair-author"])
    result = evaluate_gate("smoke_close_issue", [receipt], manifest_bundle_sha256=load_manifest_bundle()["manifest_bundle_sha256"],
                           gate_evaluator_ref="repair-author")
    assert result["decision"] != "approved"
    (tmp_path / "source.txt").write_text("changed after measurement")
    assert gate([receipt])["rejection_basis"] == "invalid_stale"


def test_screenshot_is_content_addressed_without_overriding_failed_ui(monkeypatch):
    from agent_training.uve.adapters import adb
    from agent_training.uve.evidence_store import EvidenceStore
    spec = {"adb_executable": "adb", "adb_serial": "unused", "timeout_seconds": 1}
    def load(ref):
        return spec if ref == "adb" else {"screenshot": True}
    monkeypatch.setattr(adb, "load_json_ref", load)
    monkeypatch.setattr(adb, "_run_ui_flow", lambda *args: {"verdict": "failed", "fault_origin": "subject_fault"})
    png = b"\x89PNG\r\n\x1a\nfixture"
    monkeypatch.setattr(adb.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, png, b""))
    result = adb.run_probe(probe_id="adb_ui_flow", ctx={"profile": {"adb_probe_ref": "adb", "ui_flow_ref": "flow"}, "role_id": "uve_runner"})
    assert result["verdict"] == "failed"
    assert result["artifacts"][0]["media_type"] == "image/png"
    assert result["artifacts"][0]["content_sha256"].startswith("sha256:")


def test_lifecycle_refuses_to_stop_an_existing_transport(monkeypatch):
    from agent_training.uve.adapters import adb_lifecycle
    spec = {"adb_executable": "adb", "adb_serial": "emulator-1234", "command_timeout_seconds": 1}
    monkeypatch.setattr(adb_lifecycle, "load_json_ref", lambda ref: spec)
    monkeypatch.setattr(adb_lifecycle.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, "List of devices attached\nemulator-1234 device\n", ""))
    def must_not_launch(*args, **kwargs):
        raise AssertionError("must not launch or stop an occupied serial")
    monkeypatch.setattr(adb_lifecycle.subprocess, "Popen", must_not_launch)
    result = adb_lifecycle.run_probe(probe_id="adb_lifecycle", ctx={"profile": {"adb_lifecycle_ref": "spec"},
                                      "role_id": "uve_runner", "probe_run_id": "occupied-serial-test"})
    assert result["verdict"] == "not_verified"
    assert "already in use" in result["reason"]


def test_deletion_plans_only_relevant_changes(tmp_path, monkeypatch):
    from agent_training.uve.identity import build_inventory_records
    from agent_training.uve import planner
    ref, profile = local_profile(tmp_path, monkeypatch)
    before = build_inventory_records(tmp_path, profile)
    (tmp_path / "source.txt").unlink()
    monkeypatch.setattr(planner, "load_json_ref", lambda ref: {"rules": [
        {"rule_id": "text", "path_globs": ["*.txt"], "probe_ids": ["command_check"]}]})
    assert planner.plan_probes_for_changes(profile, tmp_path, before) == ["command_check"]


def test_symlink_escape_fails_closed(tmp_path):
    from agent_training.uve.identity import build_inventory_records
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    try:
        (workspace / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("host does not allow creating test symlinks")
    with pytest.raises(ValueError):
        build_inventory_records(workspace, {"identity": {"watch_globs": ["*.txt"]}})


def test_atomic_receipt_replace_retries_transient_windows_reader(tmp_path, monkeypatch):
    from agent_training.uve.bundle import write_json_atomic
    original = Path.replace
    calls = []
    def reader_busy(path, target):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("reader holds old file")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", reader_busy)
    output = tmp_path / "state.json"
    write_json_atomic(output, {"engine_degraded": False})
    assert len(calls) == 2
    assert json.loads(output.read_text()) == {"engine_degraded": False}
    assert not list(tmp_path.glob("*.tmp"))


def test_historical_receipt_cannot_satisfy_live_checkpoint_gate(monkeypatch):
    fixture = pipeline_root() / "config/uve/fixtures/tier_v1_minimal.json"
    monkeypatch.setenv("UVE_AI_LAB_TIER_JSON", str(fixture))
    receipt = run_probe(SMOKE, "ai_lab_tier")
    result = evaluate_gate("ai_lab_checkpoint", [receipt], manifest_bundle_sha256=load_manifest_bundle()["manifest_bundle_sha256"],
                           gate_evaluator_ref="independent")
    assert result["decision"] == "not_verified"


def test_lifecycle_uses_stop_budget_and_requires_transport_disappearance(monkeypatch):
    from agent_training.uve.adapters import adb_lifecycle
    spec = {"adb_executable": "adb", "adb_serial": "unused", "avd_id": "fixture",
            "emulator_executable_env": "UVE_TEST_EMULATOR", "launch_argv": [],
            "command_timeout_seconds": 1, "stop_timeout_seconds": 9, "boot_timeout_seconds": 10,
            "poll_interval_seconds": 0.01, "boot_property": "sys.boot_completed", "expected_boot_value": "1"}
    monkeypatch.setenv("UVE_TEST_EMULATOR", "fixture")
    monkeypatch.setattr(adb_lifecycle, "load_json_ref", lambda ref: spec)
    class Process:
        stopped = False
        returncode = 0
        def poll(self): return 0 if self.stopped else None
        def wait(self, timeout): self.stopped = True
    process = Process()
    monkeypatch.setattr(adb_lifecycle.subprocess, "Popen", lambda *args, **kwargs: process)
    def execute(argv, **kwargs):
        output = "List of devices attached\n" if argv[-1] == "devices" else b"\x89PNG\r\n\x1a\nfixture"
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(adb_lifecycle.subprocess, "run", execute)
    stop_budgets = []
    def adb(spec, args, timeout):
        if args == ["emu", "kill"]:
            stop_budgets.append(timeout)
            return subprocess.CompletedProcess(args, 0, "OK", "")
        if "getprop" in args:
            return subprocess.CompletedProcess(args, 0, "1", "")
        return subprocess.CompletedProcess(args, 1 if process.stopped else 0, "device", "")
    monkeypatch.setattr(adb_lifecycle, "_adb_run", adb)
    import uuid
    result = adb_lifecycle.run_probe(probe_id="adb_lifecycle", ctx={"profile": {"adb_lifecycle_ref": "spec"},
        "role_id": "uve_runner", "probe_run_id": uuid.uuid4().hex})
    assert result["verdict"] == "passed"
    assert stop_budgets == [9]
    assert result["lifecycle_steps"][-1]["transport_disappeared"] is True
