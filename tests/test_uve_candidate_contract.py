"""Trusted witness tests, executed against an isolated candidate package."""
from copy import deepcopy

from agent_training.uve.bundle import load_manifest_bundle
from agent_training.uve.engine_state import write_engine_state
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe

PROFILE = "config/uve/profiles/emulator_workstation_smoke.json"


def test_candidate_observes_subject_failure_and_blocks_self_approval():
    write_engine_state(engine_degraded=False, probe_run_id="witness-contract")
    probe = run_probe(PROFILE, "dry_echo", dry_mode="fail_subject")
    assert probe["verdict"] == "failed"
    assert probe["fault_origin"] == "subject_fault"
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    gate = evaluate_gate("smoke_close_issue", [probe], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "rejected"
    assert gate["rejection_basis"] == "subject_observed"
    gate = evaluate_gate("smoke_close_issue", [probe], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="author", repair_actor_refs=["author"])
    assert gate["decision"] != "approved"


def test_candidate_pass_and_tampered_binding():
    write_engine_state(engine_degraded=False, probe_run_id="witness-contract")
    probe = run_probe(PROFILE, "dry_echo")
    assert probe["verdict"] == "passed"
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    gate = evaluate_gate("smoke_close_issue", [probe], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "approved"
    stale = deepcopy(probe)
    stale["probe_plan_ref"] = "stale-bundle"
    gate = evaluate_gate("smoke_close_issue", [stale], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "rejected"
