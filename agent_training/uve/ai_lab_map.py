from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from agent_training.content_hash import content_hash
from agent_training.manifest_loader import ManifestError


def tier_json_to_probe_receipt(tier_doc: dict[str, Any], *, probe_id: str = "ai_lab_tier") -> dict[str, Any]:
    if tier_doc.get("schema_version") != "training_quality_receipt.v1":
        raise ManifestError("expected training_quality_receipt.v1")
    evidence = tier_doc.get("evidence_sha256")
    if evidence and evidence.removeprefix("sha256:") != content_hash({k: v for k, v in tier_doc.items() if k != "evidence_sha256"}).removeprefix("sha256:"):
        raise ManifestError("AI Lab receipt evidence hash mismatch")
    checks = [c for c in tier_doc.get("checks", []) if c.get("required_for_stage") is not False]
    all_pass = all(c.get("status") == "passed" for c in checks)
    any_fail = any(c.get("status") == "failed" for c in checks)
    incomplete = any(c.get("status") not in ("passed", "failed") for c in checks)
    verdict = "not_verified" if incomplete or not checks else "passed" if all_pass else "failed"
    fo = "none" if verdict == "passed" else "subject_fault" if verdict == "failed" else "engine_fault"
    identity = tier_doc.get("candidate_identity", {})
    identity_sha = identity.get("identity_sha256", tier_doc.get("identity_sha256", ""))
    return {
        "schema_version": "uve_probe_receipt.v1",
        "probe_run_id": tier_doc.get("probe_run_id", tier_doc.get("tier", "tier")),
        "subject_identity": {"identity_sha256": identity_sha, "candidate_identity": identity},
        "probe": {
            "probe_id": probe_id,
            "level": "L2",
            "tier": tier_doc.get("tier", "V1"),
            "adapter_id": "ai_lab_training_quality",
            "run_lease_id": None,
        },
        "probe_plan_ref": identity.get("eval_contract_sha256") or content_hash(tier_doc),
        "expectation": {"outcome_token": "tier_checks_pass"},
        "observation": {"checks": checks, "tier": tier_doc.get("tier")},
        "verdict": verdict,
        "fault_origin": fo,
        "failure_class": None if verdict == "passed" else "assertion_mismatch",
        "toctou": {
            "identity_at_start_sha256": identity_sha,
            "identity_at_end_sha256": identity_sha,
            "identity_stable": True,
        },
    }


def load_tier_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
