"""Read-only status projection for the GUI and CI, apart from explicit indexing."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import ManifestError, load_json_ref
from agent_training.uve.bundle import configuration_hash, resolve_log_dir, write_json_atomic
from agent_training.uve.gate_evaluator import validate_stored_gate
from agent_training.uve.identity import build_subject_identity
from agent_training.uve.workspace import resolve_workspace_root


def _policy() -> dict:
    return load_json_ref("config/uve/uve_deep_flow_manifest.json")


def _directory(policy: dict) -> Path:
    return resolve_log_dir() / policy["status_store_relative"]


def _index_path(receipt: dict) -> Path:
    identity = receipt["subject_identity"]
    key = content_hash([identity["project_id"], identity["environment"], receipt.get("profile_ref"),
                        receipt["probe"]["probe_id"]]).split(":", 1)[1]
    return _directory(_policy()) / f"{key}.json"


def index_probe(receipt: dict) -> None:
    write_json_atomic(_index_path(receipt), {"probe_run_id": receipt["probe_run_id"]})


def index_gate(gate: dict, probes: list[dict]) -> None:
    for probe in probes:
        if probe["probe"]["adapter_id"] != "deep_flow":
            continue
        path = _directory(_policy()) / "gates" / f"{uuid.UUID(probe['probe_run_id'])}.json"
        write_json_atomic(path, {"gate_ref": gate["trust_chain_ref"]})


def _load_receipt(directory: str, receipt_id: str) -> dict:
    if str(uuid.UUID(receipt_id)) != receipt_id:
        raise ValueError("invalid flow receipt reference")
    return json.loads((resolve_log_dir() / directory / f"{receipt_id}.json").read_text(encoding="utf-8"))


def collect_deep_status(manifest_bundle_sha256: str) -> list[dict]:
    policy = _policy()
    latest = sorted(_directory(policy).glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    results = []
    for path in latest[:policy["status_max_profiles"]]:
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
            receipt = _load_receipt("probes", index["probe_run_id"])
            gate_index = _directory(policy) / "gates" / f"{uuid.UUID(index['probe_run_id'])}.json"
            gate_ref = json.loads(gate_index.read_text(encoding="utf-8"))["gate_ref"] if gate_index.is_file() else None
            observation = receipt["observation"]
            ref = receipt.get("profile_ref")
            identity = receipt["subject_identity"]
            fresh = False
            if ref and identity["manifest_bundle_sha256"] == manifest_bundle_sha256:
                profile = load_json_ref(ref)
                current = build_subject_identity(resolve_workspace_root(profile), profile, manifest_bundle_sha256)
                fresh = (configuration_hash(ref) == identity.get("configuration_sha256") and
                         all(identity.get(key) == current[key] for key in current if key != "identity_sha256"))
            decision = "not_verified"
            if fresh and gate_ref:
                gate = _load_receipt("gates", gate_ref)
                if any(ref["probe_run_id"] == receipt["probe_run_id"] for ref in gate["probe_receipt_refs"]):
                    if validate_stored_gate(gate, expected_decision=gate["decision"]):
                        decision = gate["decision"]
            results.append({"flow_id": observation.get("flow_id"), "project_id": identity["project_id"],
                            "environment": identity["environment"], "tier": receipt["probe"]["tier"], "fresh": fresh,
                            "probe_run_id": receipt["probe_run_id"], "probe_verdict": receipt["verdict"],
                            "gate_decision": decision, "gate_ref": gate_ref,
                            "first_finding": observation.get("triage", {}).get("primary")})
        except (OSError, ValueError, TypeError, KeyError, ManifestError):
            continue
    return results
