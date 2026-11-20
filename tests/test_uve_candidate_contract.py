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
    explanation = probe["explanation"]
    assert explanation["what"]["expectation"] == "echo_ok"
    assert explanation["how"]["mode"] == "fixture"
    assert explanation["why"]["failure_class"] == "assertion_mismatch"
    assert explanation["evidence"]["facts"]["outcome_token"] == "echo_fail"
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    gate = evaluate_gate("smoke_close_issue", [probe], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "rejected"
    assert gate["rejection_basis"] == "subject_observed"
    assert gate["explanation"]["why"]["code"] == "subject_assertion_failed"
    assert gate["explanation"]["why"]["blocking_probe_ids"] == ["dry_echo"]
    gate = evaluate_gate("smoke_close_issue", [probe], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="author", repair_actor_refs=["author"])
    assert gate["decision"] != "approved"
    assert gate["explanation"]["why"]["code"] == "evaluator_not_independent"


def test_candidate_pass_and_tampered_binding():
    write_engine_state(engine_degraded=False, probe_run_id="witness-contract")
    probe = run_probe(PROFILE, "dry_echo")
    assert probe["verdict"] == "passed"
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    gate = evaluate_gate("smoke_close_issue", [probe], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "approved"
    assert gate["explanation"]["why"]["code"] == "requirements_met"
    legacy = deepcopy(probe)
    legacy.pop("explanation")
    assert evaluate_gate("smoke_close_issue", [legacy], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")["decision"] == "approved"
    stale = deepcopy(probe)
    stale["probe_plan_ref"] = "stale-bundle"
    gate = evaluate_gate("smoke_close_issue", [stale], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "rejected"
    assert gate["explanation"]["why"]["code"] == "probe_bundle_mismatch"
    malformed = {**probe, "probe": None}
    gate = evaluate_gate("smoke_close_issue", [malformed], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "rejected"
    assert gate["explanation"]["why"]["code"] == "invalid_probe_receipt"
    assert "missing probe" in gate["explanation"]["why"]["details"]["validation_errors"]


def test_missing_probe_gate_and_lease_denied_explain_why(monkeypatch):
    from agent_training.uve import orchestrator

    write_engine_state(engine_degraded=False, probe_run_id="witness-contract")
    bundle = load_manifest_bundle()["manifest_bundle_sha256"]
    gate = evaluate_gate("smoke_close_issue", [], manifest_bundle_sha256=bundle,
                         gate_evaluator_ref="independent-contract")
    assert gate["decision"] == "not_verified"
    assert gate["explanation"]["why"]["missing_probe_ids"] == ["dry_echo"]
    monkeypatch.setattr(orchestrator, "acquire_probe_lease", lambda *args, **kwargs: (
        None, {"verdict": "not_verified", "fault_origin": "unknown", "failure_class": "lease_denied",
               "reason": "Bearer fixture-secret", "resource_kind": "fixture"},
    ))
    probe = run_probe(PROFILE, "dry_echo", repair_actor_refs=["witness-fixture"])
    assert probe["explanation"]["how"]["mode"] == "lease_denied"
    assert probe["explanation"]["why"]["code"] == "lease_denied"
    assert probe["explanation"]["why"]["reported_reason"] == "Bearer <AUTH_TOKEN_1>"
    assert probe["repair_actor_refs"] == ["witness-fixture"]
