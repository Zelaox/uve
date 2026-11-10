from __future__ import annotations

import json
import os

from agent_training.manifest_loader import pipeline_root
from agent_training.uve.engine_state import write_engine_state
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe
from agent_training.uve.planner import plan_probes_for_changes
from agent_training.uve.recovery import run_recovery_drill
from agent_training.uve.status import collect_uve_status
from agent_training.uve.workspace import resolve_workspace_root


SMOKE = "config/uve/profiles/emulator_workstation_smoke.json"


def test_plan_probes_default_when_no_prior_inventory():
    profile_doc = json.loads(
        __import__("pathlib").Path("config/uve/profiles/emulator_workstation_smoke.json").read_text(
            encoding="utf-8"
        )
    )
    root = resolve_workspace_root(profile_doc)
    planned = plan_probes_for_changes(profile_doc, root, None)
    assert "engine_self_check" in planned


def test_gate_not_verified_when_engine_degraded():
    write_engine_state(engine_degraded=True, probe_run_id="test-degraded")
    try:
        receipt = run_probe(SMOKE, "dry_echo")
        from agent_training.uve.bundle import load_manifest_bundle

        bundle = load_manifest_bundle()
        gate = evaluate_gate(
            "repair_verification",
            [receipt],
            manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
            gate_evaluator_ref="degraded_test",
        )
        assert gate["decision"] == "not_verified"
        assert gate.get("rejection_basis") == "engine_fault"
    finally:
        write_engine_state(engine_degraded=False, probe_run_id="test-recover")


def test_adb_ui_flow_dry_fail(monkeypatch):
    from agent_training.uve.adapters import adb as adb_adapter

    os.environ["UVE_ANDROID_EXAMPLE_ROOT"] = str(pipeline_root())
    original_load = adb_adapter.load_json_ref

    def load_with_serial(ref: str):
        doc = original_load(ref)
        if ref.endswith("adb_probe.json"):
            return {**doc, "adb_serial": "emulator-5554"}
        return doc

    monkeypatch.setattr(adb_adapter, "load_json_ref", load_with_serial)
    receipt = run_probe(
        "config/uve/profiles/android_example_development.json",
        "adb_ui_flow",
        dry_mode="ui_fail",
    )
    assert receipt["verdict"] == "failed"
    assert receipt["observation"].get("ui_steps")


def test_recovery_denied_for_smoke_profile():
    out = run_recovery_drill(SMOKE, "restart_metro")
    assert out["status"] == "denied"


def test_uve_status_collects_engine_block():
    status = collect_uve_status()
    assert "engine" in status
    assert status["engine"]["engine_degraded"] is False


def test_probe_failure_syncs_issue_when_profile_enabled():
    receipt = run_probe(SMOKE, "dry_echo", dry_mode="fail_subject")
    assert receipt["verdict"] == "failed"
    issue_path = pipeline_root() / "logs/uve/issues" / f"probe_{receipt['probe_run_id']}.json"
    assert issue_path.is_file()
