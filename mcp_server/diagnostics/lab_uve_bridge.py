"""Load and validate Lab ↔ UVE triage bridge manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BRIDGE_MANIFEST_REL = "config/lab_uve_triage_bridge_manifest.json"


class LabUveBridgeError(Exception):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LabUveBridgeError(f"missing manifest: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LabUveBridgeError(f"invalid manifest: {path}") from exc
    if not isinstance(data, dict):
        raise LabUveBridgeError(f"manifest must be object: {path}")
    return data


def load_ref(repo_root: Path, ref: str) -> dict[str, Any]:
    path = (repo_root / ref).resolve()
    if not str(path).startswith(str(repo_root.resolve())):
        raise LabUveBridgeError(f"manifest ref escapes repo: {ref}")
    return _load_json(path)


def load_bridge_manifest(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    bridge = _load_json(repo_root / BRIDGE_MANIFEST_REL)
    for key, ref in bridge.items():
        if not key.endswith("_ref"):
            continue
        if not isinstance(ref, str):
            raise LabUveBridgeError(f"{key} must be a string ref")
        load_ref(repo_root, ref)
    return bridge


def resolve_report_root(repo_root: Path, bridge: dict[str, Any]) -> Path:
    spec = load_ref(repo_root, bridge["report_output_root_ref"])
    rel = spec.get("relative_path")
    if not rel:
        raise LabUveBridgeError("report_output_root missing relative_path")
    return (repo_root / rel).resolve()


def assert_write_allowed(repo_root: Path, bridge: dict[str, Any], target: Path) -> None:
    repo_root = repo_root.resolve()
    report_root = resolve_report_root(repo_root, bridge)
    target = target.resolve()
    if not str(target).startswith(str(report_root)):
        raise LabUveBridgeError(
            f"write outside report catalog: {target} (allowed root {report_root})"
        )
    protected = load_ref(repo_root, bridge["protected_source_roots_ref"])
    for rel in protected.get("roots_relative") or []:
        prot = (repo_root / rel).resolve()
        if str(target).startswith(str(prot)):
            raise LabUveBridgeError(f"write forbidden under protected root {rel}")


def training_pipeline_root(repo_root: Path, bridge: dict[str, Any]) -> Path:
    binding = load_ref(repo_root, bridge["uve_binding_ref"])
    rel = binding.get("training_pipeline_relative_from_repo")
    if not rel:
        raise LabUveBridgeError("uve_binding missing training_pipeline_relative_from_repo")
    return (repo_root / rel).resolve()
