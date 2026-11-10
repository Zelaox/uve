from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from agent_training.content_hash import canonical_json_bytes
from agent_training.manifest_loader import load_json_ref
from agent_training.uve.leases import LeaseManager

_groups: dict[str, list[tuple[str, str, str]]] = {}


def resources_for_probe(profile: dict, probe_id: str) -> list[tuple[str, str]]:
    specs = profile.get("lease_requirements", {}).get(probe_id, [])
    if isinstance(specs, dict):
        specs = [specs]
    catalog = load_json_ref("config/uve/uve_resource_lock_manifest.json")["resource_kinds"]
    result = []
    for spec in specs:
        kind = spec["resource_kind"]
        policy = catalog[kind]
        values = dict(profile.get("isolation", {}))
        for ref_field in policy.get("ref_fields", []):
            if spec.get(ref_field):
                values.update(load_json_ref(spec[ref_field]))
        values.update(spec.get("key_values", {}))
        fields = spec.get("key_fields", policy["key_fields"])
        if spec.get("path_key_fields"):
            from agent_training.uve.bundle import resolve_log_dir
            from agent_training.uve.workspace import resolve_workspace_root

            for field in spec["path_key_fields"]:
                if field not in fields or field not in policy.get("path_key_fields", []):
                    raise ValueError("lease path field is not declared by the resource policy")
                values[field] = str(Path(values[field].format(log_root=resolve_log_dir(),
                                                            workspace_root=resolve_workspace_root(profile))).resolve())
        key = canonical_json_bytes({field: values[field] for field in fields}).decode("utf-8")
        result.append((kind, key))
    return sorted(set(result))


def acquire_probe_lease(profile: dict, probe_id: str, holder: str) -> tuple[str | None, dict[str, Any] | None]:
    resources = resources_for_probe(profile, probe_id)
    if not resources:
        return None, None
    lm = LeaseManager()
    acquired = []
    for kind, key in resources:
        token = lm.acquire(kind, key, holder)
        if token is None:
            for previous_kind, previous_key, previous_token in acquired:
                lm.release(previous_kind, previous_key, previous_token)
            return None, {"verdict": "not_verified", "fault_origin": "unknown",
                          "failure_class": "lease_denied", "resource_kind": kind, "key": key}
        acquired.append((kind, key, token))
    group = str(uuid.uuid4())
    _groups[group] = acquired
    return group, None


def owns_probe_lease(profile: dict, probe_id: str, lease_id: str) -> bool:
    held = _groups.get(lease_id, [])
    lm = LeaseManager()
    return (set(resources_for_probe(profile, probe_id)) <= {(k, v) for k, v, _ in held}
            and all(lm.owns(k, v, token) for k, v, token in held))


def release_probe_lease(profile: dict, probe_id: str, lease_id: str | None) -> None:
    lm = LeaseManager()
    for kind, key, token in _groups.pop(lease_id, []):
        lm.release(kind, key, token)
