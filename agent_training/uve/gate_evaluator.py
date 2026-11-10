from __future__ import annotations

import uuid
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref
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

    if read_engine_state().get("engine_degraded"):
        return _gate_receipt(
            gate_id,
            gate_evaluator_ref,
            probe_receipts,
            manifest_bundle_sha256,
            decision="not_verified",
            rejection_basis="engine_fault",
        )
    try:
        gate_manifest = load_json_ref(gate_manifest_ref)
    except Exception:
        return _gate_receipt(gate_id, gate_evaluator_ref, probe_receipts, manifest_bundle_sha256,
                             decision="not_verified", rejection_basis="engine_fault")
    spec = gate_manifest["gates"].get(gate_id)
    if not spec:
        raise KeyError(gate_id)

    def reject(basis: str, decision: str = "rejected") -> dict[str, Any]:
        return _gate_receipt(gate_id, gate_evaluator_ref, probe_receipts,
                             manifest_bundle_sha256, decision=decision, rejection_basis=basis)

    actors = set(repair_actor_refs or [])
    for receipt in probe_receipts:
        actors.update(receipt.get("repair_actor_refs", []))
    if not gate_evaluator_ref or gate_evaluator_ref in actors:
        return reject("engine_fault", "not_verified")
    from agent_training.uve.bundle import load_manifest_bundle, configuration_hash
    try:
        trusted_bundle = load_manifest_bundle()
    except Exception:
        return reject("engine_fault", "not_verified")
    if trusted_bundle["manifest_bundle_sha256"] != manifest_bundle_sha256:
        return reject("manifest_mismatch")
    if gate_manifest["_content_hash"] != trusted_bundle["_loaded"]["config/uve/uve_gate_manifest.json"]["_content_hash"]:
        return reject("manifest_mismatch")
    for receipt in probe_receipts:
        if validate_probe_receipt(receipt):
            return reject("invalid_stale")
        plan = receipt["probe_plan_ref"]
        recorded_bundle = plan.get("manifest_bundle_sha256") if isinstance(plan, dict) else plan
        if recorded_bundle != manifest_bundle_sha256:
            return reject("manifest_mismatch")
        if receipt.get("profile_ref"):
            try:
                if configuration_hash(receipt["profile_ref"]) != receipt["subject_identity"].get("configuration_sha256"):
                    return reject("manifest_mismatch")
                from agent_training.uve.identity import build_subject_identity
                from agent_training.uve.workspace import resolve_workspace_root
                profile = load_json_ref(receipt["profile_ref"])
                current = build_subject_identity(resolve_workspace_root(profile), profile, manifest_bundle_sha256)
                recorded = receipt["subject_identity"]
                if any(current.get(key) != recorded.get(key) for key in current if key != "identity_sha256"):
                    return reject("invalid_stale")
            except Exception:
                return reject("invalid_stale")
        binding = receipt.get("execution_binding", {})
        if receipt["verdict"] == "passed":
            if binding.get("snapshot_mode") == "required" and not receipt["toctou"].get("snapshot_binding_sha256"):
                return reject("invalid_stale")
            if receipt["probe"]["probe_id"] in binding.get("runtime_delivery_binding_probe_ids", []):
                if not receipt["toctou"].get("runtime_delivery_binding_sha256"):
                    return reject("invalid_stale")

    required = spec["required_probe_ids"]
    by_probe = {p["probe"]["probe_id"]: p for p in probe_receipts}
    if len(by_probe) != len(probe_receipts):
        return reject("invalid_stale")
    missing = [pid for pid in required if pid not in by_probe]
    if missing:
        return _gate_receipt(
            gate_id,
            gate_evaluator_ref,
            probe_receipts,
            manifest_bundle_sha256,
            decision="not_verified",
            rejection_basis=None,
        )

    if spec.get("require_flow_replay"):
        from agent_training.uve.flow_replay import replay

        for probe_id in required:
            if by_probe[probe_id]["verdict"] == "not_verified":
                continue
            try:
                if not replay(by_probe[probe_id], spec["evaluator_role"])["matches_receipt"]:
                    return reject("invalid_stale")
            except Exception:
                return reject("invalid_stale")

    for pid, expectations in spec.get("required_observation_values", {}).items():
        if pid not in by_probe or any(by_probe[pid].get("observation", {}).get(key) != value for key, value in expectations.items()):
            return reject("insufficient_probes", "not_verified")

    identity_sha = probe_receipts[0]["subject_identity"]["identity_sha256"]
    for pr in probe_receipts:
        if pr["subject_identity"]["identity_sha256"] != identity_sha:
            return _gate_receipt(
                gate_id,
                gate_evaluator_ref,
                probe_receipts,
                manifest_bundle_sha256,
                decision="rejected",
                rejection_basis="invalid_stale",
            )

    any_not_verified = any(p["verdict"] == "not_verified" for p in probe_receipts if p["probe"]["probe_id"] in required)
    if any_not_verified:
        return _gate_receipt(
            gate_id,
            gate_evaluator_ref,
            probe_receipts,
            manifest_bundle_sha256,
            decision="not_verified",
            rejection_basis="insufficient_probes",
        )

    if spec.get("require_identity_stable"):
        unstable = any(
            not p.get("toctou", {}).get("identity_stable", True)
            for p in probe_receipts
            if p["probe"]["probe_id"] in required
        )
        if unstable:
            return _gate_receipt(
                gate_id,
                gate_evaluator_ref,
                probe_receipts,
                manifest_bundle_sha256,
                decision="rejected",
                rejection_basis="identity_unstable",
            )

    failed_subject = [
        p
        for p in probe_receipts
        if p["probe"]["probe_id"] in required
        and p["verdict"] == "failed"
        and p.get("fault_origin") == "subject_fault"
    ]
    all_pass = all(
        p["verdict"] == "passed"
        for p in probe_receipts
        if p["probe"]["probe_id"] in required
    )

    if all_pass:
        return _gate_receipt(
            gate_id,
            gate_evaluator_ref,
            probe_receipts,
            manifest_bundle_sha256,
            decision="approved",
            rejection_basis=None,
        )

    if failed_subject:
        return _gate_receipt(
            gate_id,
            gate_evaluator_ref,
            probe_receipts,
            manifest_bundle_sha256,
            decision="rejected",
            rejection_basis="subject_observed",
        )

    return _gate_receipt(
        gate_id,
        gate_evaluator_ref,
        probe_receipts,
        manifest_bundle_sha256,
        decision="rejected",
        rejection_basis="engine_fault",
    )


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
