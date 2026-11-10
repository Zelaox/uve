from __future__ import annotations

import json
import json
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.issue_store import IssueStore
from agent_training.manifest_loader import load_json_ref, pipeline_root
from agent_training.uve.bundle import write_json_atomic


def _storage_dir() -> Path:
    bridge = load_json_ref("config/uve/uve_issue_bridge_manifest.json")
    return pipeline_root() / bridge["issue_storage_relative"]


def sync_probe_to_issue(probe_receipt: dict[str, Any], project_id: str) -> str | None:
    bridge = load_json_ref("config/uve/uve_issue_bridge_manifest.json")
    if probe_receipt.get("verdict") not in bridge["open_on_probe_verdicts"]:
        return None
    if probe_receipt["probe"].get("adapter_id") == "deep_flow" and bridge.get("flow_fingerprints_relative"):
        return _sync_flow_issue(probe_receipt, project_id, bridge)
    store = IssueStore(_storage_dir(), bridge["contract_ref"])
    obs = (
        f"UVE probe {probe_receipt['probe']['probe_id']} "
        f"verdict={probe_receipt['verdict']} failure_class={probe_receipt.get('failure_class')}"
    )
    record = store.create_issue(
        project_id=project_id,
        observation=obs,
        repro_steps=[probe_receipt["probe_run_id"]],
        artifact_hashes=[content_hash(probe_receipt)],
    )
    store.append_evidence(
        record.issue_id,
        bridge["evidence_type_probe"],
        [content_hash(probe_receipt)],
        verifier_ref=probe_receipt["probe_run_id"],
        pass_fail=False,
    )
    write_json_atomic(
        pipeline_root() / "logs/uve/issues" / f"probe_{probe_receipt['probe_run_id']}.json",
        {"issue_id": record.issue_id, "probe_run_id": probe_receipt["probe_run_id"]},
    )
    return record.issue_id


def _sync_flow_issue(receipt: dict, project_id: str, bridge: dict) -> str | None:
    from agent_training.uve.flow_replay import triage
    from agent_training.uve.leases import LeaseManager

    diagnosis = triage(receipt)
    fingerprint = diagnosis["finding_fingerprint"]
    if not fingerprint:
        return None
    key = fingerprint.split(":", 1)[1]
    manager = LeaseManager()
    kind = bridge["flow_fingerprint_resource_kind"]
    acquired = manager.acquire(kind, key, receipt["probe_run_id"])
    if acquired is None:
        return None  # The original immutable probe remains available for reconciliation.
    try:
        index = _storage_dir() / bridge["flow_fingerprints_relative"] / f"{key}.json"
        store = IssueStore(_storage_dir(), bridge["contract_ref"])
        record = store.load(json.loads(index.read_text(encoding="utf-8"))["issue_id"]) if index.is_file() else None
        if record is None or record.resolution.status == "closed":
            record = store.create_issue(project_id, f"UVE flow finding {diagnosis['primary']['rule_id']}",
                                        [receipt["probe_run_id"]], [content_hash(receipt)])
            write_json_atomic(index, {"issue_id": record.issue_id, "finding_fingerprint": fingerprint})
        if not any(item.verifier_ref == receipt["probe_run_id"] for item in record.evidence):
            store.append_evidence(record.issue_id, bridge["evidence_type_probe"], [content_hash(receipt)],
                                  verifier_ref=receipt["probe_run_id"], pass_fail=False)
        return record.issue_id
    finally:
        manager.release(kind, key, acquired)


def sync_gate_to_issue(gate_receipt: dict[str, Any], project_id: str) -> str | None:
    bridge = load_json_ref("config/uve/uve_issue_bridge_manifest.json")
    if gate_receipt.get("decision") not in bridge["open_on_gate_decisions"]:
        return None
    store = IssueStore(_storage_dir(), bridge["contract_ref"])
    obs = f"UVE gate {gate_receipt['gate_id']} decision={gate_receipt['decision']}"
    record = store.create_issue(
        project_id=project_id,
        observation=obs,
        repro_steps=[gate_receipt.get("trust_chain_ref", "")],
        artifact_hashes=[content_hash(gate_receipt)],
    )
    store.append_evidence(
        record.issue_id,
        bridge["evidence_type_gate"],
        [content_hash(gate_receipt)],
        verifier_ref=gate_receipt.get("trust_chain_ref", gate_receipt["gate_id"]),
        pass_fail=False,
    )
    return record.issue_id
