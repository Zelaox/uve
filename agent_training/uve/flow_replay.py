"""Replay a projected flow artifact and diagnose a real UVE receipt."""

from __future__ import annotations

import json

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref
from agent_training.uve.bundle import configuration_hash, load_manifest_bundle
from agent_training.uve.evidence_store import EvidenceStore
from agent_training.uve.flow_contract import compile_flow, require
from agent_training.uve.flow_result import evaluate
from agent_training.uve.receipts import assert_valid_probe


def replay(receipt: dict, role_id: str) -> dict:
    assert_valid_probe(receipt)
    bundle = load_manifest_bundle()
    require(receipt["probe_plan_ref"].get("manifest_bundle_sha256") == bundle["manifest_bundle_sha256"], "flow policy has changed")
    ref = receipt["profile_ref"]
    require(configuration_hash(ref) == receipt["probe_plan_ref"]["configuration_sha256"], "flow configuration has changed")
    profile = load_json_ref(ref)
    catalog = bundle["_loaded"]["config/uve/uve_probe_catalog.json"]
    spec = catalog["probes"][receipt["probe"]["probe_id"]]
    require(spec["adapter_id"] == "deep_flow", "receipt does not reference a deep flow")
    policy = bundle["_loaded"][spec["deep_flow_policy_ref"]]
    plan = compile_flow(profile["deep_flow_ref"], policy, spec["depth"])
    observation = receipt["observation"]
    artifact = observation["flow_evidence"]
    require(artifact in receipt.get("artifacts", []), "flow evidence is not linked to this receipt")
    raw = EvidenceStore().read_artifact(role_id, artifact, maximum_bytes=policy["budgets"]["evidence_bytes"])
    stored = json.loads(raw.decode("utf-8"))
    require(stored.get("schema_version") == "uve_deep_flow_evidence.v1", "unsupported flow evidence")
    require(stored["probe_run_id"] == receipt["probe_run_id"], "flow evidence is from a different run")
    require(stored["plan_sha256"] == plan["plan_sha256"] == observation["deep_flow_plan_sha256"], "flow plan mismatch")
    result = evaluate(plan, stored["evidence"], policy)
    return {**result, "probe_run_id": receipt["probe_run_id"], "flow_id": plan["flow_id"], "depth": plan["depth"],
            "timeline": {key: {"events": value["events"], "duration_ms": value["duration_ms"]}
                         for key, value in stored["evidence"].items()},
            "matches_receipt": result["verdict"] == receipt["verdict"] and result["cases"] == observation["cases"]}


def registry_matches(receipt: dict, registry: dict) -> list[dict]:
    observation = receipt.get("observation", {})
    coordinates = {"failureClass": receipt.get("failure_class"), "faultRule": observation.get("fault_rule"),
                   "probeId": receipt.get("probe", {}).get("probe_id")}
    return [entry for entry in registry.get("entries", []) if entry.get("match") and
            all(value is not None and coordinates.get(key) == value for key, value in entry["match"].items())]


def compare(left: dict, right: dict, role_id: str) -> dict:
    require(left["subject_identity"]["identity_sha256"] == right["subject_identity"]["identity_sha256"],
            "different candidates cannot establish a reproduction")
    require(left["observation"]["deep_flow_plan_sha256"] == right["observation"]["deep_flow_plan_sha256"],
            "different flow recipes cannot establish a reproduction")
    before, after = replay(left, role_id), replay(right, role_id)
    require(before["matches_receipt"] and after["matches_receipt"], "replay did not match its receipt")
    return {"left": left["probe_run_id"], "right": right["probe_run_id"], "flow_id": before["flow_id"],
            "matches_receipt": before["verdict"] == after["verdict"] and before["cases"] == after["cases"],
            "left_verdict": before["verdict"], "right_verdict": after["verdict"]}


def triage(receipt: dict, registry_ref: str | None = None) -> dict:
    assert_valid_probe(receipt)
    observation = receipt["observation"]
    profile_ref = receipt.get("profile_ref")
    if registry_ref is None and profile_ref:
        registry_ref = load_json_ref(profile_ref).get("error_registry_ref")
    identity_drift = receipt["toctou"].get("identity_stable") is False
    primary = observation.get("triage", {}).get("primary")
    if identity_drift:
        primary = {"category": "binding", "rule_id": "identity_unstable", "next_probe": "fresh_bound_probe"}
    if primary is None and receipt["verdict"] != "passed":
        primary = {"category": "probe", "rule_id": receipt.get("failure_class"), "next_probe": "inspect_probe_binding"}
    matches = registry_matches(receipt, load_json_ref(registry_ref)) if registry_ref else []
    identity = receipt["subject_identity"]
    fingerprint = (content_hash([identity["project_id"], identity["environment"], observation.get("flow_id"),
                                 primary.get("subject"), primary.get("rule_id"), receipt.get("failure_class")])
                   if primary else None)
    return {"probe_run_id": receipt["probe_run_id"], "verdict": receipt["verdict"], "fault_origin": receipt["fault_origin"],
            "failure_class": receipt.get("failure_class"), "identity_drift": identity_drift, "primary": primary,
            "finding_fingerprint": fingerprint, "findings": observation.get("triage", {}).get("findings", []),
            "registry_matches": matches}
