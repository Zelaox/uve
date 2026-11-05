from __future__ import annotations

from typing import Any

from agent_training.manifest_loader import ManifestError

PROBE_VERDICTS = frozenset({"passed", "failed", "not_verified"})
FAULT_ORIGINS = frozenset({"none", "subject_fault", "engine_fault", "unknown"})
GATE_DECISIONS = frozenset({"approved", "rejected", "not_verified"})

FORBIDDEN_PROBE_KEYS = frozenset(
    {"gate_decision", "decision", "rejection_basis", "training_label"}
)


def validate_probe_receipt(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if doc.get("schema_version") != "uve_probe_receipt.v1":
        errors.append("probe receipt schema_version must be uve_probe_receipt.v1")
    for key in FORBIDDEN_PROBE_KEYS:
        if key in doc:
            errors.append(f"probe receipt must not contain {key}")
    for git_key in ("git_commit_oid", "commit_sha256", "git_object_format"):
        if git_key in doc:
            errors.append(f"probe receipt must not contain git field {git_key}")
    verdict = doc.get("verdict")
    if verdict not in PROBE_VERDICTS:
        errors.append(f"invalid verdict: {verdict}")
    if verdict == "engine_fault":
        errors.append("verdict must not be engine_fault; use fault_origin")
    fo = doc.get("fault_origin")
    if fo not in FAULT_ORIGINS:
        errors.append(f"invalid fault_origin: {fo}")
    toctou = doc.get("toctou", {})
    for field in ("probe_run_id", "subject_identity", "probe", "probe_plan_ref", "expectation", "observation", "toctou"):
        if not doc.get(field):
            errors.append(f"missing {field}")
    identity_sha = doc.get("subject_identity", {}).get("identity_sha256")
    if not identity_sha:
        errors.append("missing subject identity hash")
    for field in ("identity_at_start_sha256", "identity_at_end_sha256", "identity_stable"):
        if field not in toctou:
            errors.append(f"missing toctou.{field}")
    if toctou.get("identity_at_end_sha256") != identity_sha:
        errors.append("TOCTOU end identity must match subject identity")
    if toctou.get("identity_stable") is True and toctou.get("identity_at_start_sha256") != toctou.get("identity_at_end_sha256"):
        errors.append("identity_stable conflicts with start/end hashes")
    if verdict == "passed" and (fo != "none" or toctou.get("identity_stable") is not True):
        errors.append("passed requires fault_origin none and identity_stable true")
    if verdict == "not_verified" and fo == "subject_fault":
        errors.append("not_verified cannot claim subject_fault")
    if verdict == "failed" and fo not in ("subject_fault", "engine_fault"):
        errors.append("failed requires subject_fault or engine_fault")
    if toctou.get("identity_stable") is False and verdict == "passed":
        errors.append("passed forbidden when identity_stable is false")
    if "subject_identity" not in doc:
        errors.append("missing subject_identity")
    return errors


def validate_gate_receipt(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if doc.get("schema_version") != "uve_gate_receipt.v1":
        errors.append("gate receipt schema_version must be uve_gate_receipt.v1")
    decision = doc.get("decision")
    if decision not in GATE_DECISIONS:
        errors.append(f"invalid gate decision: {decision}")
    if decision == "rejected" and not doc.get("rejection_basis"):
        errors.append("rejected gate requires rejection_basis")
    if decision == "approved" and not doc.get("probe_receipt_refs"):
        errors.append("gate receipt requires probe_receipt_refs")
    if not doc.get("gate_evaluator_ref"):
        errors.append("gate requires independent evaluator")
    return errors


def assert_valid_probe(doc: dict[str, Any]) -> None:
    errors = validate_probe_receipt(doc)
    if errors:
        raise ManifestError("; ".join(errors))


def assert_valid_gate(doc: dict[str, Any]) -> None:
    errors = validate_gate_receipt(doc)
    if errors:
        raise ManifestError("; ".join(errors))
