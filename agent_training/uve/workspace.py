from __future__ import annotations

import os
from pathlib import Path

from agent_training.manifest_loader import ManifestError, load_json_ref, pipeline_root


def resolve_workspace_root(profile: dict) -> Path:
    if profile.get("project_root_ref"):
        root_spec = load_json_ref(profile["project_root_ref"])
        if root_spec.get("context_ref"):
            context = load_json_ref(root_spec["context_ref"])
            if str(context.get("owner_pid")) != str(root_spec["owner_pid"]):
                raise ManifestError("workspace context belongs to a different owner")
            raw = context.get(root_spec["root_field"])
            if not raw or not Path(raw).is_absolute() or not Path(raw).is_dir():
                raise ManifestError("selected workspace is unavailable")
        else:
            env_key = root_spec["root_env"]
            raw = os.environ.get(env_key)
        if not raw:
            raise ManifestError("workspace root binding is not set")
        base = Path(raw).resolve()
    else:
        base = pipeline_root()
    rel = profile.get("workspace_root_relative", ".")
    return (base / rel).resolve()
