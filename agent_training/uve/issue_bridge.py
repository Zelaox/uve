from __future__ import annotations

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
