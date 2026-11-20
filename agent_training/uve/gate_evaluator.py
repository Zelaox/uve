from __future__ import annotations

import uuid
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref
from agent_training.uve.receipt_explanation import explain_gate, probe_identifier
from agent_training.uve.receipts import assert_valid_gate, validate_probe_receipt


def validate_stored_gate(gate: dict[str, Any], *, expected_decision: str = "approved") -> bool:
    """Revalidate receipt hashes, current inputs and policy at consumption time."""
    from agent_training.uve.bundle import resolve_log_dir
    import json

    try:
        assert_valid_gate(gate)
        probes = []
        for ref in gate["probe_receipt_refs"]:
            path = resolve_log_dir() / "probes" / f"{ref['probe_run_id']}.json"
            probe = json.loads(path.read_text(encoding="utf-8"))
            if content_hash({k: v for k, v in probe.items() if not k.startswith("_")}) != ref["receipt_sha256"]:
                return False
            if probe["subject_identity"]["identity_sha256"] != gate["subject_identity_sha256"]:
                return False
            probes.append(probe)
        fresh = evaluate_gate(gate["gate_id"], probes,
                              manifest_bundle_sha256=gate["manifest_bundle_sha256"],
                              gate_evaluator_ref=gate["gate_evaluator_ref"])
        return gate["decision"] == expected_decision == fresh["decision"]
    except Exception:
        return False


def evaluate_gate(
    gate_id: str,
    probe_receipts: list[dict[str, Any]],
    *,
    manifest_bundle_sha256: str,
    gate_evaluator_ref: str,
    gate_manifest_ref: str = "config/uve/uve_gate_manifest.json",
    repair_actor_refs: list[str] | None = None,
) -> dict[str, Any]:
    from agent_training.uve.engine_state import read_engine_state

    spec: dict[str, Any] | None = None

    def result(reason: str, basis: str | None = None, decision: str = "rejected",
               *, probes: list[str] | None = None, details: dict | None = None) -> dict[str, Any]:
        receipt = _gate_receipt(gate_id, gate_evaluator_ref, probe_receipts,
                                manifest_bundle_sha256, decision=decision, rejection_basis=basis)
        receipt["explanation"] = explain_gate(
            receipt, probe_receipts, reason_code=reason, spec=spec,
            blocking_probe_ids=probes, details=details,
        )
        return receipt

    if read_engine_state().get("engine_degraded"):
        return result("engine_degraded", "engine_fault", "not_verified")
    try:
        gate_manifest = load_json_ref(gate_manifest_ref)
    except Exception:
        return result("gate_manifest_unavailable", "engine_fault", "not_verified")
    spec = gate_manifest["gates"].get(gate_id)
    if not spec:
        raise KeyError(gate_id)

    actors = set(repair_actor_refs or [])
    for receipt in probe_receipts:
        actors.update(receipt.get("repair_actor_refs", []))
    if not gate_evaluator_ref or gate_evaluator_ref in actors:
        return result("evaluator_not_independent", "engine_fault", "not_verified")
    from agent_training.uve.bundle import load_manifest_bundle, configuration_hash
    try:
        trusted_bundle = load_manifest_bundle()
    except Exception:
        return result("bundle_unavailable", "engine_fault", "not_verified")
    if trusted_bundle["manifest_bundle_sha256"] != manifest_bundle_sha256:
        return result("bundle_mismatch", "manifest_mismatch")
    if gate_manifest["_content_hash"] != trusted_bundle["_loaded"]["config/uve/uve_gate_manifest.json"]["_content_hash"]:
        return result("gate_policy_mismatch", "manifest_mismatch")
    for receipt in probe_receipts:
        probe_id = probe_identifier(receipt)
        blocked = [probe_id] if probe_id else []
        errors = validate_probe_receipt(receipt)
        if errors:
            return result("invalid_probe_receipt", "invalid_stale", probes=blocked,
                          details={"validation_error_count": len(errors), "validation_errors": errors})
        plan = receipt["probe_plan_ref"]
        recorded_bundle = plan.get("manifest_bundle_sha256") if isinstance(plan, dict) else plan
        if recorded_bundle != manifest_bundle_sha256:
            return result("probe_bundle_mismatch", "manifest_mismatch", probes=blocked)
        if receipt.get("profile_ref"):
            try:
                if configuration_hash(receipt["profile_ref"]) != receipt["subject_identity"].get("configuration_sha256"):
                    return result("probe_configuration_changed", "manifest_mismatch", probes=blocked)
                from agent_training.uve.identity import build_subject_identity
                from agent_training.uve.workspace import resolve_workspace_root
                profile = load_json_ref(receipt["profile_ref"])
                current = build_subject_identity(resolve_workspace_root(profile), profile, manifest_bundle_sha256)
                recorded = receipt["subject_identity"]
                if any(current.get(key) != recorded.get(key) for key in current if key != "identity_sha256"):
                    return result("probe_identity_changed", "invalid_stale", probes=blocked)
            except Exception:
                return result("probe_binding_unavailable", "invalid_stale", probes=blocked)
        binding = receipt.get("execution_binding", {})
        if receipt["verdict"] == "passed":
            if binding.get("snapshot_mode") == "required" and not receipt["toctou"].get("snapshot_binding_sha256"):
                return result("snapshot_proof_missing", "invalid_stale", probes=blocked)
            if receipt["probe"]["probe_id"] in binding.get("runtime_delivery_binding_probe_ids", []):
                if not receipt["toctou"].get("runtime_delivery_binding_sha256"):
                    return result("delivery_proof_missing", "invalid_stale", probes=blocked)

    required = spec["required_probe_ids"]
    by_probe = {p["probe"]["probe_id"]: p for p in probe_receipts}
    if len(by_probe) != len(probe_receipts):
        from collections import Counter

        duplicate_ids = [pid for pid, count in Counter(p["probe"]["probe_id"] for p in probe_receipts).items()
                         if count > 1]
        return result("duplicate_probe", "invalid_stale", probes=duplicate_ids)
    missing = [pid for pid in required if pid not in by_probe]
    if missing:
        return result("required_probes_missing", decision="not_verified", probes=missing)

    if spec.get("require_flow_replay"):
        from agent_training.uve.flow_replay import replay

        for probe_id in required:
            if by_probe[probe_id]["verdict"] == "not_verified":
                continue
            try:
                if not replay(by_probe[probe_id], spec["evaluator_role"])["matches_receipt"]:
                    return result("flow_replay_mismatch", "invalid_stale", probes=[probe_id])
            except Exception:
                return result("flow_replay_mismatch", "invalid_stale", probes=[probe_id])

    for pid, expectations in spec.get("required_observation_values", {}).items():
        observed = by_probe.get(pid, {}).get("observation", {})
        mismatched = [key for key, value in expectations.items() if observed.get(key) != value]
        if pid not in by_probe or mismatched:
            return result("required_observation_mismatch", "insufficient_probes", "not_verified",
                          probes=[pid], details={"observation_fields": mismatched})

    identity_sha = probe_receipts[0]["subject_identity"]["identity_sha256"]
    for pr in probe_receipts:
        if pr["subject_identity"]["identity_sha256"] != identity_sha:
            return result("subject_identity_mismatch", "invalid_stale", probes=[pr["probe"]["probe_id"]])

    unverified = [pid for pid in required if by_probe[pid]["verdict"] == "not_verified"]
    if unverified:
        return result("required_probe_not_verified", "insufficient_probes", "not_verified", probes=unverified)

    if spec.get("require_identity_stable"):
        unstable = [pid for pid in required if not by_probe[pid].get("toctou", {}).get("identity_stable", True)]
        if unstable:
            return result("required_probe_identity_unstable", "identity_unstable", probes=unstable)

    if all(by_probe[pid]["verdict"] == "passed" for pid in required):
        return result("requirements_met", decision="approved")

    failed_subject = [pid for pid in required if by_probe[pid]["verdict"] == "failed"
                      and by_probe[pid].get("fault_origin") == "subject_fault"]
    if failed_subject:
        return result("subject_assertion_failed", "subject_observed", probes=failed_subject)

    failed = [pid for pid in required if by_probe[pid]["verdict"] != "passed"]
    return result("probe_execution_failed", "engine_fault", probes=failed)


def _gate_receipt(
    gate_id: str,
    gate_evaluator_ref: str,
    probe_receipts: list[dict[str, Any]],
    manifest_bundle_sha256: str,
    *,
    decision: str,
    rejection_basis: str | None,
) -> dict[str, Any]:
    refs = [
        {
            "probe_run_id": p["probe_run_id"],
            "receipt_sha256": content_hash({k: p[k] for k in sorted(p) if not k.startswith("_")}),
        }
        for p in probe_receipts
    ]
    doc: dict[str, Any] = {
        "schema_version": "uve_gate_receipt.v1",
        "gate_id": gate_id,
        "gate_evaluator_ref": gate_evaluator_ref,
        "probe_receipt_refs": refs,
        "subject_identity_sha256": probe_receipts[0]["subject_identity"]["identity_sha256"]
        if probe_receipts
        else "",
        "decision": decision,
        "manifest_bundle_sha256": manifest_bundle_sha256,
        "trust_chain_ref": str(uuid.uuid4()),
    }
    if rejection_basis:
        doc["rejection_basis"] = rejection_basis
    assert_valid_gate(doc)
    return doc
