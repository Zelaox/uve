from __future__ import annotations

from agent_training.uve.regression import capture_baseline, compare_baseline
from agent_training.uve.witness import run_witness_bundle


def test_witness_bundle_approves():
    result = run_witness_bundle()
    assert result["approved"] is True
    assert result["gate"]["decision"] == "approved"


def test_uve_gate_verifier_after_witness():
    from agent_training.lab.verifiers import run_verifier

    run_witness_bundle()
    assert run_verifier("uve_smoke_gate", "config/manifests/task_registry.json", seed=0)


def test_regression_baseline_stable():
    capture_baseline("emulator_workstation_smoke")
    cmp = compare_baseline("emulator_workstation_smoke")
    assert cmp["regressed"] is False
