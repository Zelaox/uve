from __future__ import annotations

import hashlib
import os
import fnmatch
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref


def _normalize_relative(path: Path, workspace_root: Path) -> str:
    path.resolve().relative_to(workspace_root.resolve())
    rel = path.absolute().relative_to(workspace_root.absolute())
    parts = []
    for p in rel.parts:
        if p == "..":
            raise ValueError("path escapes workspace")
        parts.append(p.replace("\\", "/"))
    return "/".join(parts)


def matches_glob(relative_path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(relative_path, pattern):
        return True
    if "**/" in pattern:
        return matches_glob(relative_path, pattern.replace("**/", "", 1))
    return False


def _file_kind(path: Path, identity_manifest: dict[str, Any]) -> str:
    ext = path.suffix.lower()
    mapping = identity_manifest.get("file_kind_by_extension", {})
    return mapping.get(ext, identity_manifest.get("default_file_kind", "asset"))


def _hash_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _iter_watched_files(
    workspace_root: Path,
    watch_globs: list[str],
    ignore_globs: list[str],
    identity_manifest: dict[str, Any],
) -> list[Path]:
    max_depth = int(identity_manifest["max_scan_depth"])
    max_bytes = int(identity_manifest["max_file_bytes"])
    max_entries = int(identity_manifest["max_entry_count"])
    entries: list[Path] = []
    for root, dirs, files in os.walk(workspace_root):
        for directory in list(dirs):
            candidate = Path(root) / directory
            rel_dir = candidate.relative_to(workspace_root).as_posix()
            if any(matches_glob(rel_dir + "/", ig) for ig in ignore_globs):
                dirs.remove(directory)
            elif candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                # Directory links cannot silently remove watched inputs.
                candidate.resolve().relative_to(workspace_root.resolve())
                raise ValueError(f"directory symlink requires explicit exclusion: {rel_dir}")
        depth = len(Path(root).relative_to(workspace_root).parts)
        if depth > max_depth:
            raise ValueError("inventory exceeds max_scan_depth")
        for name in files:
            full = Path(root) / name
            rel = str(full.relative_to(workspace_root)).replace("\\", "/")
            rel_path = Path(rel)
            if any(matches_glob(rel, ig) for ig in ignore_globs):
                continue
            if not any(matches_glob(rel, wg) for wg in watch_globs):
                continue
            if full.is_symlink() and identity_manifest["symlink_policy"] == "exclude":
                continue
            if full.lstat().st_size > max_bytes:
                raise ValueError(f"file exceeds max_file_bytes: {rel}")
            entries.append(full)
            if len(entries) > max_entries:
                raise ValueError("inventory exceeds max_entry_count")
    return sorted(entries, key=lambda p: _normalize_relative(p, workspace_root))


def build_inventory_records(
    workspace_root: Path,
    profile: dict[str, Any],
    identity_manifest_ref: str = "config/uve/uve_subject_identity_manifest.json",
) -> list[dict[str, Any]]:
    identity_manifest = load_json_ref(profile.get("identity_manifest_ref", identity_manifest_ref))
    watch = profile["identity"]["watch_globs"]
    for pattern in watch:
        if Path(pattern).is_absolute() or ":" in pattern or ".." in pattern.replace("\\", "/").split("/"):
            raise ValueError("watch glob must stay within the workspace")
    ignore = profile["identity"].get("ignore_globs", []) + identity_manifest.get("identity_ignore_globs", [])
    symlink_policy = identity_manifest["symlink_policy"]
    missing_policy = identity_manifest["missing_file_policy"]
    records: list[dict[str, Any]] = []
    for path in _iter_watched_files(workspace_root, watch, ignore, identity_manifest):
        rel = _normalize_relative(path, workspace_root)
        kind = _file_kind(path, identity_manifest)
        if path.is_symlink():
            if symlink_policy == "exclude":
                continue
            target = path.resolve().relative_to(workspace_root.resolve()).as_posix()
            content_sha = _hash_bytes(path.read_bytes() if symlink_policy == "follow" else target.encode("utf-8"))
            records.append(
                {
                    "relative_path": rel,
                    "file_kind": kind,
                    "content_sha256": content_sha,
                    "symlink_target": target.replace("\\", "/"),
                }
            )
            continue
        with path.open("rb") as stream:
            content_sha = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        records.append(
            {"relative_path": rel, "file_kind": kind, "content_sha256": content_sha}
        )

    for pattern in watch:
        if not any(c in pattern for c in "*?[") and not (workspace_root / pattern).exists():
            if missing_policy == "fail_inventory":
                raise ValueError(f"missing watched file: {pattern}")
            records.append({"relative_path": pattern, "file_kind": "absent", "content_sha256": _hash_bytes(b"")})
    records.sort(key=lambda r: r["relative_path"])
    return records


def workspace_files_inventory_sha256(records: list[dict[str, Any]]) -> str:
    slim = [
        {
            "relative_path": r["relative_path"],
            "file_kind": r["file_kind"],
            "content_sha256": r["content_sha256"],
        }
        for r in records
    ]
    return content_hash(slim)


def build_subject_identity(
    workspace_root: Path,
    profile: dict[str, Any],
    manifest_bundle_sha256: str,
    *,
    knowledge_base_binding_sha256: str | None = None,
) -> dict[str, Any]:
    records = build_inventory_records(workspace_root, profile)
    inv = workspace_files_inventory_sha256(records)
    bundle: dict[str, Any] = {
        "workspace_files_inventory_sha256": inv,
        "manifest_bundle_sha256": manifest_bundle_sha256,
        "project_id": profile["project_id"],
        "environment": profile["environment"],
    }
    if knowledge_base_binding_sha256:
        bundle["knowledge_base_binding_sha256"] = knowledge_base_binding_sha256
    for field, ref in (("environment_pins_sha256", profile.get("environment_pins_ref")),
                       ("knowledge_base_binding_sha256", profile.get("knowledge_base_manifest_ref"))):
        if ref:
            bundle[field] = load_json_ref(ref)["_content_hash"]
    bundle["identity_sha256"] = content_hash(
        {k: bundle[k] for k in sorted(bundle) if k != "identity_sha256"}
    )
    return bundle
