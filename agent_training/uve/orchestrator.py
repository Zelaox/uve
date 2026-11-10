from __future__ import annotations

import importlib
import uuid
from typing import Any

from agent_training.manifest_loader import ManifestError, load_json_ref
from agent_training.uve.bundle import configuration_hash, load_manifest_bundle, resolve_log_dir, write_json_atomic
from agent_training.content_hash import content_hash
from agent_training.uve.identity import build_subject_identity
from agent_training.uve.lease_policy import acquire_probe_lease, release_probe_lease, owns_probe_lease
from agent_training.uve.receipts import assert_valid_probe
from agent_training.uve.workspace import resolve_workspace_root


def load_profile(profile_ref: str) -> dict[str, Any]:
    return load_json_ref(profile_ref)


def _invoke_adapter(adapter_id: str, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    catalog = ctx["manifest_bundle"]["_loaded"]["config/uve/uve_adapter_catalog.json"]
    spec = catalog["adapters"].get(adapter_id)
    if not spec:
        raise ManifestError(f"unknown adapter: {adapter_id}")
    mod = importlib.import_module(spec["module"])
    fn = getattr(mod, spec["callable"])
    return fn(probe_id=probe_id, ctx=ctx)


def run_probe(
    profile_ref: str, probe_id: str, *, role_id: str = "uve_runner",
    run_lease_id: str | None = None, dry_mode: str | None = None,
    repair_actor_refs: list[str] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    profile = load_profile(profile_ref)
    try:
        return _run_probe(profile_ref, probe_id, role_id=role_id, run_lease_id=run_lease_id,
                          dry_mode=dry_mode, repair_actor_refs=repair_actor_refs, lease_state=state)
    finally:
        if run_lease_id is None:
            release_probe_lease(profile, probe_id, state.get("lease_id"))


def _run_probe(
    profile_ref: str,
    probe_id: str,
    *,
    role_id: str = "uve_runner",
    run_lease_id: str | None = None,
    dry_mode: str | None = None,
    repair_actor_refs: list[str] | None = None,
    lease_state: dict[str, Any],
) -> dict[str, Any]:
    bundle = load_manifest_bundle()
    profile = load_profile(profile_ref)
    probe_catalog = bundle["_loaded"]["config/uve/uve_probe_catalog.json"]
    probe_spec = probe_catalog["probes"].get(probe_id)
    if not probe_spec:
        raise KeyError(probe_id)

    workspace = resolve_workspace_root(profile)
    probe_run_id = str(uuid.uuid4())
    id_start = build_subject_identity(workspace, profile, bundle["manifest_bundle_sha256"])
    config_start = configuration_hash(profile_ref)
    id_start["configuration_sha256"] = config_start
    id_start["identity_sha256"] = content_hash({k: v for k, v in id_start.items() if k != "identity_sha256"})

    lease_id = run_lease_id
    borrowed_lease = lease_id is not None
    if borrowed_lease and not owns_probe_lease(profile, probe_id, lease_id):
        raise ManifestError("supplied lease does not own the probe resources")
    if not borrowed_lease:
        lease_id, lease_fail = acquire_probe_lease(profile, probe_id, holder=role_id)
        if lease_fail:
            lease_probe_run_id = str(uuid.uuid4())
            receipt = _lease_denied_receipt(
                lease_probe_run_id, probe_id, probe_spec, profile, bundle, id_start, lease_id, lease_fail
            )
            receipt["profile_ref"] = profile_ref
            write_json_atomic(resolve_log_dir() / "probes" / f"{lease_probe_run_id}.json", receipt)
            if probe_spec["adapter_id"] == "deep_flow":
                from agent_training.uve.flow_status import index_probe

                index_probe(receipt)
            return receipt
    lease_state["lease_id"] = lease_id

    binding = bundle["_loaded"]["config/uve/uve_execution_binding_manifest.json"]
    prof_id = profile.get("execution_binding_profile_id", "default_profile")
    bind = binding.get("profiles", {}).get(prof_id, binding["default_profile"])
    probe_workspace = workspace
    snapshot_binding = None
    ctx = {
        "profile": profile,
        "probe_spec": probe_spec,
        "workspace_root": probe_workspace,
        "role_id": role_id,
        "manifest_bundle_sha256": bundle["manifest_bundle_sha256"],
        "probe_run_id": probe_run_id,
        "run_lease_id": lease_id,
        "manifest_bundle": bundle,
    }
    if dry_mode:
        ctx["dry_mode"] = dry_mode

    try:
        if bind.get("snapshot_mode") == "required":
            from agent_training.uve.snapshot import materialize_snapshot, snapshot_dir

            snap = materialize_snapshot(workspace, profile, snapshot_id=probe_run_id)
            snapshot_binding = snap["snapshot_binding_sha256"]
            probe_workspace = snapshot_dir(snap["snapshot_id"])
            ctx["workspace_root"] = probe_workspace
        observation = _invoke_adapter(probe_spec["adapter_id"], probe_id, ctx)
        if snapshot_binding:
            from agent_training.uve.identity import build_inventory_records, workspace_files_inventory_sha256
            if workspace_files_inventory_sha256(build_inventory_records(probe_workspace, profile)) != snapshot_binding:
                observation = {"verdict": "not_verified", "fault_origin": "engine_fault",
                               "failure_class": "identity_unstable", "reason": "snapshot changed during probe"}
    except Exception as exc:
        observation = {"verdict": "not_verified", "fault_origin": "engine_fault",
                       "failure_class": "adapter_error", "error": f"{type(exc).__name__}: {exc}"}

    end_error = None
    try:
        id_end = build_subject_identity(workspace, profile, bundle["manifest_bundle_sha256"])
        id_end["configuration_sha256"] = configuration_hash(profile_ref)
        id_end["identity_sha256"] = content_hash({k: v for k, v in id_end.items() if k != "identity_sha256"})
        bundle_stable = load_manifest_bundle()["manifest_bundle_sha256"] == bundle["manifest_bundle_sha256"]
    except Exception as exc:
        id_end = dict(id_start)
        bundle_stable = False
        end_error = str(exc)
    candidate = observation.pop("candidate_identity", None)
    if candidate:
        for identity in (id_start, id_end):
            identity["candidate_identity"] = candidate
            identity["identity_sha256"] = content_hash({k: v for k, v in identity.items() if k != "identity_sha256"})
    identity_stable = end_error is None and id_start["identity_sha256"] == id_end["identity_sha256"]
    if end_error:
        observation["end_identity_error"] = end_error

    observation = dict(observation)
    verdict = observation.pop("verdict", "not_verified")
    fault_origin = observation.pop("fault_origin", "unknown")
    if not identity_stable:
        verdict = "not_verified"
        fault_origin = "engine_fault"
        observation["identity_drift"] = True
    if not bundle_stable:
        verdict, fault_origin = "not_verified", "engine_fault"
        observation["failure_class"] = "manifest_mismatch"
    if verdict == "passed" and probe_id in bind.get("runtime_delivery_binding_probe_ids", []):
        if not observation.get("runtime_delivery_binding_sha256"):
            verdict, fault_origin = "not_verified", "engine_fault"
            observation["failure_class"] = "binding_missing"

    if bind.get("require_identity_stable_for_pass") and verdict == "passed" and not identity_stable:
        verdict = "not_verified"

    receipt: dict[str, Any] = {
        "schema_version": "uve_probe_receipt.v1",
        "probe_run_id": probe_run_id,
        "subject_identity": id_end,
        "probe": {
            "probe_id": probe_id,
            "level": probe_spec["level"],
            "tier": probe_spec["tier"],
            "adapter_id": probe_spec["adapter_id"],
            "run_lease_id": lease_id,
        },
        "probe_plan_ref": {"manifest_bundle_sha256": bundle["manifest_bundle_sha256"],
                           "configuration_sha256": config_start,
                           "plan_sha256": content_hash([probe_id, probe_spec, config_start, bundle["manifest_bundle_sha256"]])},
        "profile_ref": profile_ref,
        "execution_binding": bind,
        "repair_actor_refs": list(repair_actor_refs or []),
        "expectation": load_json_ref(probe_spec["expectation_ref"]),
        "observation": observation,
        "artifacts": observation.pop("artifacts", []),
        "verdict": verdict,
        "fault_origin": fault_origin if verdict != "passed" else "none",
        "failure_class": observation.get("failure_class"),
        "toctou": {
            "identity_at_start_sha256": id_start["identity_sha256"],
            "identity_at_end_sha256": id_end["identity_sha256"],
            "identity_stable": identity_stable,
            "snapshot_binding_sha256": snapshot_binding or observation.get("snapshot_binding_sha256"),
            "runtime_delivery_binding_sha256": observation.get("runtime_delivery_binding_sha256"),
        },
    }
    assert_valid_probe(receipt)
    write_json_atomic(resolve_log_dir() / "probes" / f"{probe_run_id}.json", receipt)
    if probe_spec["adapter_id"] == "deep_flow":
        from agent_training.uve.flow_status import index_probe

        index_probe(receipt)
    if profile.get("sync_issues_on_failure") and receipt["verdict"] in ("failed", "not_verified"):
        from agent_training.uve.issue_bridge import sync_probe_to_issue

        sync_probe_to_issue(receipt, profile["project_id"])
    return receipt


def _lease_denied_receipt(
    probe_run_id: str,
    probe_id: str,
    probe_spec: dict[str, Any],
    profile: dict[str, Any],
    bundle: dict[str, Any],
    subject_identity: dict[str, Any],
    lease_id: str | None,
    lease_fail: dict[str, Any],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": "uve_probe_receipt.v1",
        "probe_run_id": probe_run_id,
        "subject_identity": subject_identity,
        "probe": {
            "probe_id": probe_id,
            "level": probe_spec["level"],
            "tier": probe_spec["tier"],
            "adapter_id": probe_spec["adapter_id"],
            "run_lease_id": lease_id,
        },
        "probe_plan_ref": bundle["manifest_bundle_sha256"],
        "expectation": load_json_ref(probe_spec["expectation_ref"]),
        "observation": lease_fail,
        "verdict": lease_fail["verdict"],
        "fault_origin": lease_fail.get("fault_origin", "unknown"),
        "failure_class": lease_fail.get("failure_class"),
        "toctou": {
            "identity_at_start_sha256": subject_identity["identity_sha256"],
            "identity_at_end_sha256": subject_identity["identity_sha256"],
            "identity_stable": True,
        },
    }
    assert_valid_probe(receipt)
    return receipt
