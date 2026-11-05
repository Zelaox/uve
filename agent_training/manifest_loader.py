from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash_file_text
from agent_training.paths import pipeline_root


class ManifestError(Exception):
    pass


def resolve_config_path(ref: str, *, must_be_file: bool = True) -> Path:
    root = pipeline_root()
    path = Path(ref)
    if not path.is_absolute():
        path = root / ref
    if must_be_file:
        if not path.is_file():
            raise ManifestError(f"manifest ref not found: {ref} -> {path}")
    elif not path.exists():
        raise ManifestError(f"manifest path not found: {ref} -> {path}")
    return path


def load_json_ref(ref: str) -> dict[str, Any]:
    path = resolve_config_path(ref)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ManifestError(f"expected JSON object: {path}")
    try:
        manifest_path = path.relative_to(pipeline_root())
    except ValueError:
        manifest_path = path
    data["_manifest_path"] = str(manifest_path).replace("\\", "/")
    data["_content_hash"] = content_hash_file_text(text)
    return data


def load_json_path(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ManifestError(f"expected JSON object: {path}")
    root = pipeline_root()
    try:
        rel = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = str(path)
    data["_manifest_path"] = rel
    data["_content_hash"] = content_hash_file_text(text)
    return data
