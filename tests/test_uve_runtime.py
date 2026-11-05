from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_training.manifest_loader import ManifestError, load_json_ref
from agent_training.uve import runtime
from agent_training.uve.leases import LeaseManager

RUNTIME = "config/uve/uve_workstation_runtime.json"


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "resolve_log_dir", lambda: tmp_path / "evidence")
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"owner_pid": os.getpid(), "sdk_root": "", "adb_serial": "",
                                "guest_package": "", "project_root": ""}), encoding="utf-8")
    return path


def test_standalone_tiers_never_invoke_ai_lab(context, monkeypatch):
    from agent_training.uve import orchestrator
    original = orchestrator._invoke_adapter
    seen = []

    def invoke(adapter, probe, ctx):
        assert adapter in {"engine_meta", "command", "adb"}
        seen.append(adapter)
        return original(adapter, probe, ctx)

    monkeypatch.setenv("AI_LAB_ROOT", "unavailable AI Lab checkout")
    monkeypatch.setattr(orchestrator, "_invoke_adapter", invoke)
    profile = runtime.materialize_runtime_profile(RUNTIME, context, os.getpid())
    result = runtime.run_runtime_cycle(RUNTIME, profile)
    assert [tier["tier_id"] for tier in result["tiers"]] == ["V1", "V2", "V3"]
    assert [tier["gate"]["decision"] for tier in result["tiers"]] == ["approved", "not_verified", "not_verified"]
    assert seen == ["engine_meta", "command", "adb", "adb"]
    for tier in result["tiers"][1:]:
        for probe in tier["probes"]:
            assert probe["observation"]["missing_fields"] == ["adb_serial"]
            assert probe["fault_origin"] == "unknown"
            assert probe["receipt_path"].endswith(f"{probe['probe_run_id']}.json")
        assert tier["gate_receipt_path"].endswith(f"{tier['gate']['trust_chain_ref']}.json")


def test_context_rebinds_serial_package_and_unicode_paths(context):
    values = json.loads(context.read_text())
    values.update(sdk_root="C:/SDK å/工具", adb_serial="selected-device", guest_package="org.example.selected")
    context.write_text(json.dumps(values), encoding="utf-8")
    ref = runtime.materialize_runtime_profile(RUNTIME, context, os.getpid())
    profile = load_json_ref(ref)
    target = load_json_ref(profile["adb_probe_ref"])
    assert target["adb_serial"] == values["adb_serial"]
    assert target["adb_executable"].startswith(values["sdk_root"])
    assert load_json_ref(profile["guest_package_ref"])["package_substring"] == values["guest_package"]
    command = load_json_ref(profile["command_probe_refs"]["workstation_transport"])
    assert command["argv"][0] == "{adb_executable}"
    values.update(adb_serial="next-device", guest_package="")
    context.write_text(json.dumps(values), encoding="utf-8")
    runtime.materialize_runtime_profile(RUNTIME, context, os.getpid())
    assert load_json_ref(profile["adb_probe_ref"])["adb_serial"] == "next-device"
    assert load_json_ref(profile["guest_package_ref"])["package_substring"] == ""
    with pytest.raises(ManifestError, match="different process"):
        runtime.materialize_runtime_profile(RUNTIME, context, os.getpid() + 1)


@pytest.mark.parametrize("field,value", [("allowed_adapter_ids", []), ("tiers", [
    {"tier_id": "V1", "probe_ids": ["ai_lab_tier"], "gate_id": "ai_lab_checkpoint"}])])
def test_invalid_startup_policy_rejected_before_any_probe(context, tmp_path, monkeypatch, field, value):
    spec = load_json_ref(RUNTIME)
    spec[field] = value
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(runtime, "run_probe", lambda *a, **k: pytest.fail("must reject before executing adapters"))
    with pytest.raises(ManifestError):
        runtime.run_runtime_cycle(str(path), "unused")


def test_worker_runs_on_change_and_stops_releasing_lease(context, tmp_path, monkeypatch):
    spec = load_json_ref(RUNTIME)
    spec.update(poll_interval_seconds=0.001, verification_interval_seconds=1000)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    cycles = []
    sleeps = []
    stop = tmp_path / "stop"

    def cycle(*args):
        cycles.append(args)
        return {"tiers": []}

    def sleep(*args):
        sleeps.append(args)
        if len(sleeps) == 2:
            values = json.loads(context.read_text())
            values["adb_serial"] = "changed-selection"
            context.write_text(json.dumps(values), encoding="utf-8")
        if len(sleeps) == 3:
            stop.touch()

    monkeypatch.setattr(runtime, "run_runtime_cycle", cycle)
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    status = runtime.serve_runtime(str(path), context, os.getpid(), stop, tmp_path / "status.json", parent_alive=lambda: True)
    assert len(cycles) == 2
    assert status["state"] == "stopped"
    manager = LeaseManager()
    key = os.path.normcase(str(context.resolve()))
    token = manager.acquire(spec["lease_resource_kind"], key, "next")
    assert token
    manager.release(spec["lease_resource_kind"], key, token)


def test_parent_exit_stops_worker_and_duplicate_is_denied(context, tmp_path, monkeypatch):
    spec = load_json_ref(RUNTIME)
    manager = LeaseManager()
    key = os.path.normcase(str(context.resolve()))
    token = manager.acquire(spec["lease_resource_kind"], key, "other")
    try:
        status = runtime.serve_runtime(RUNTIME, context, os.getpid(), tmp_path / "stop", tmp_path / "status.json")
        assert status["state"] == "not_verified"
    finally:
        manager.release(spec["lease_resource_kind"], key, token)
    monkeypatch.setattr(runtime, "run_runtime_cycle", lambda *a: pytest.fail("owner has exited"))
    status = runtime.serve_runtime(RUNTIME, context, os.getpid(), tmp_path / "stop", tmp_path / "status.json", parent_alive=lambda: False)
    assert status["state"] == "stopped"


def test_status_write_failure_still_releases_owner_handle_and_lease(context, tmp_path, monkeypatch):
    closed = []
    original_close = runtime.ParentProcess.close
    def close(self):
        closed.append(self.pid)
        original_close(self)
    monkeypatch.setattr(runtime.ParentProcess, "close", close)
    monkeypatch.setattr(runtime, "_write_changed", lambda *a: (_ for _ in ()).throw(OSError("read-only status")))
    with pytest.raises(OSError):
        runtime.serve_runtime(RUNTIME, context, os.getpid(), tmp_path / "stop", tmp_path / "status.json", parent_alive=lambda: False)
    assert closed == [os.getpid()]
    spec = load_json_ref(RUNTIME)
    manager = LeaseManager()
    key = os.path.normcase(str(context.resolve()))
    token = manager.acquire(spec["lease_resource_kind"], key, "next")
    assert token
    manager.release(spec["lease_resource_kind"], key, token)


def test_missing_package_cannot_pass_ui_assertion(tmp_path, monkeypatch):
    from agent_training.uve.adapters import adb
    package = tmp_path / "package.json"
    package.write_text('{"package_substring":""}')
    flow = tmp_path / "flow.json"
    flow.write_text(json.dumps({"steps": [{"step_id": "focus", "kind": "shell_dump_window",
                                         "assertion": "package_present", "package_substring_ref": str(package)}]}))
    monkeypatch.setattr(adb, "_adb_run", lambda *a: pytest.fail("no selected application"))
    assert adb._run_ui_flow({"timeout_seconds": 1}, str(flow))["verdict"] == "not_verified"


def test_parent_handle_tracks_real_process():
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    owner = runtime.ParentProcess(child.pid)
    try:
        assert owner.alive()
        child.terminate()
        child.wait(timeout=5)
        assert not owner.alive()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        owner.close()
