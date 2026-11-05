from __future__ import annotations

import re
import json
from typing import Any

from agent_training.manifest_loader import ManifestError, load_json_ref


def apply_secret_placeholders(text: str, placeholder_ref: str) -> str:
    manifest = load_json_ref(placeholder_ref)
    counters: dict[str, int] = {}
    instances: dict[tuple[str, str], int] = {}
    out = text
    for pat in manifest["patterns"]:
        kind = pat["token_kind"]
        counters.setdefault(kind, 0)

        def repl(match: re.Match[str], p=pat, k=kind) -> str:
            key = (k, match.group(0))
            if key not in instances:
                counters[k] += 1
                instances[key] = counters[k]
            inst = instances[key]
            tpl = p["placeholder_template"]
            if "\\1" in tpl:
                return tpl.replace("\\1", match.group(1)).replace("{instance}", str(inst))
            return tpl.replace("{instance}", str(inst))

        out = re.sub(pat["regex"], repl, out)
    return out


def derive_training_label(
    gate_receipt: dict[str, Any],
    probe_receipts: list[dict[str, Any]],
    required_probe_ids: list[str],
    export_ref: str = "config/uve/uve_training_export_manifest.json",
) -> str:
    export_manifest = load_json_ref(export_ref)
    decision = gate_receipt.get("decision")
    probes = [p for p in probe_receipts if p["probe"]["probe_id"] in required_probe_ids]
    if set(required_probe_ids) != {p["probe"]["probe_id"] for p in probes} or len(probes) != len(set(required_probe_ids)):
        raise ManifestError("training label requires exactly one receipt for each required probe")

    if export_manifest["forbidden"].get("verified_labels_when_any_required_probe_not_verified"):
        if any(p["verdict"] == "not_verified" for p in probes):
            if decision == "approved":
                raise ManifestError("cannot label verified_success with not_verified probe")
            return "unverified_proposal"

    if decision == "approved":
        rules = export_manifest["rules"]["verified_success"]
        if any(p["verdict"] != "passed" for p in probes):
            raise ManifestError("verified_success requires all probes passed")
        if rules.get("require_identity_stable") and any(
            not p.get("toctou", {}).get("identity_stable", True) for p in probes
        ):
            raise ManifestError("verified_success requires identity_stable")
        return "verified_success"

    if decision == "rejected" and gate_receipt.get("rejection_basis") == "subject_observed":
        if any(p.get("toctou", {}).get("identity_stable") is not True for p in probes):
            return "unverified_proposal"
        if any(p["verdict"] == "not_verified" for p in probes):
            return "unverified_proposal"
        rules = export_manifest["rules"]["verified_failure"]
        failed = [p for p in probes if p["verdict"] == "failed" and p.get("fault_origin") == "subject_fault"]
        if not failed:
            raise ManifestError("verified_failure requires subject_fault failed probe")
        return "verified_failure"

    return "unverified_proposal"


def build_export_row(
    *,
    failure_class: str,
    gate_receipt: dict[str, Any],
    probe_receipts: list[dict[str, Any]],
    required_probe_ids: list[str],
    fields: dict[str, Any],
) -> dict[str, Any]:
    from agent_training.content_hash import content_hash
    from agent_training.uve.receipts import assert_valid_gate, assert_valid_probe

    assert_valid_gate(gate_receipt)
    refs = {r["probe_run_id"]: r["receipt_sha256"] for r in gate_receipt["probe_receipt_refs"]}
    if set(refs) != {p["probe_run_id"] for p in probe_receipts}:
        raise ManifestError("gate references do not match export receipts")
    for probe in probe_receipts:
        assert_valid_probe(probe)
        if refs[probe["probe_run_id"]] != content_hash({k: v for k, v in probe.items() if not k.startswith("_")}):
            raise ManifestError("probe content hash does not match gate")
        if probe["subject_identity"]["identity_sha256"] != gate_receipt["subject_identity_sha256"]:
            raise ManifestError("gate and probe identity mismatch")
    label = derive_training_label(gate_receipt, probe_receipts, required_probe_ids)
    protected = {"schema_version", "data_tier", "gate_receipt_ref", "failure_class", "training_label", "verification", "subject_identity_sha256"}
    if protected.intersection(fields):
        raise ManifestError("export fields cannot override verification metadata")
    row = {
        "schema_version": "uve_training_export_row.v1",
        "data_tier": "training_dataset",
        "gate_receipt_ref": gate_receipt.get("trust_chain_ref"),
        "failure_class": failure_class,
        "training_label": label,
        "verification": {
            "probe_receipt_refs": [p["probe_run_id"] for p in probe_receipts],
            "gate_receipt_ref": gate_receipt.get("trust_chain_ref"),
            "gate_decision": gate_receipt.get("decision"),
            "gate_rejection_basis": gate_receipt.get("rejection_basis"),
        },
        "subject_identity_sha256": gate_receipt.get("subject_identity_sha256"),
        **fields,
    }
    return json.loads(apply_secret_placeholders(json.dumps(row, ensure_ascii=False),
                                               "config/uve/uve_secret_placeholder_manifest.json"))
