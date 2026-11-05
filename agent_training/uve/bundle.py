from __future__ import annotations

import json
import uuid
import time
from pathlib import Path
from typing import Any

from agent_training.content_hash import canonical_json_bytes, content_hash
from agent_training.manifest_loader import ManifestError, load_json_ref, pipeline_root


def load_engine_manifest(ref: str = "config/uve/uve_engine_manifest.json") -> dict[str, Any]:
    return load_json_ref(ref)


def load_manifest_bundle(bundle_ref: str | None = None) -> dict[str, Any]:
    engine = load_engine_manifest()
    ref = bundle_ref or engine["bundle_ref"]
    bundle = load_json_ref(ref)
    docs: dict[str, Any] = {}
    def visit(manifest_ref: str) -> None:
        if manifest_ref in docs:
            return
        doc = load_json_ref(manifest_ref)
        docs[manifest_ref] = doc
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith("_ref") and isinstance(child, str) and child.endswith(".json"):
                        visit(child)
                    elif not key.startswith("_"):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(doc)
    for manifest_ref in bundle["manifest_refs"]:
        visit(manifest_ref)
    bundle["_loaded"] = docs
    bundle["manifest_bundle_sha256"] = content_hash(
        {k: docs[k]["_content_hash"] for k in sorted(docs)}
    )
    return bundle


def configuration_hash(profile_ref: str) -> str:
    """Bind profiles and every transitively referenced JSON policy/target."""
    docs: dict[str, str] = {}

    def visit(ref: str) -> None:
        if ref in docs:
            return
        doc = load_json_ref(ref)
        docs[ref] = doc["_content_hash"]

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith("_ref") and isinstance(child, str) and child.endswith(".json"):
                        visit(child)
                    elif key.endswith("_refs") and isinstance(child, dict):
                        for reference in child.values():
                            if isinstance(reference, str) and reference.endswith(".json"):
                                visit(reference)
                    elif not key.startswith("_"):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(doc)

    visit(profile_ref)
    # Reference locations are lookup data, not machine-dependent hash inputs.
    return content_hash(sorted(docs.values()))


def resolve_log_dir(engine_ref: str = "config/uve/uve_engine_manifest.json") -> Path:
    engine = load_engine_manifest(engine_ref)
    log_root = load_json_ref(engine["log_root_ref"])
    root = pipeline_root()
    rel = log_root["log_dir"]
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    policy = load_json_ref(load_engine_manifest()["io_policy_ref"])
    attempts = int(policy["replace_attempts"])
    if attempts < 1:
        raise ManifestError("replace_attempts must be positive")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(attempts):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(policy["replace_retry_interval_ms"] / 1000)
    finally:
        tmp.unlink(missing_ok=True)
