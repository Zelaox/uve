from __future__ import annotations

import json

from agent_training.uve.bundle import load_manifest_bundle, resolve_log_dir
from agent_training.uve.export_training import build_export_row
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe


def test_synthetic_fault_repair_export_pipeline():
    profile = "config/uve/profiles/emulator_workstation_smoke.json"
    fail_receipt = run_probe(profile, "dry_echo")
    assert fail_receipt["verdict"] == "passed"

    bundle = load_manifest_bundle()
    gate = evaluate_gate(
        "repair_verification",
        [fail_receipt],
        manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
        gate_evaluator_ref="e2e_test",
    )
    assert gate["decision"] == "approved"

    row = build_export_row(
        failure_class="undefined_property",
        gate_receipt=gate,
        probe_receipts=[fail_receipt],
        required_probe_ids=["dry_echo"],
        fields={
            "source_relative_path": "TrainingPlanner.tsx",
            "source_line": 184,
            "error_summary": "Cannot read properties of undefined",
            "repair_attempt_summary": "Add null guard",
        },
    )
    assert row["training_label"] == "verified_success"
    assert row["verification"]["gate_decision"] == "approved"

    out = resolve_log_dir() / "e2e_export_row.json"
    out.write_text(json.dumps(row, indent=2), encoding="utf-8")
