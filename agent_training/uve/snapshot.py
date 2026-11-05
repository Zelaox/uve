from __future__ import annotations

import hashlib
import shutil
import stat
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import pipeline_root, load_json_ref
from agent_training.uve.identity import build_inventory_records, workspace_files_inventory_sha256


def materialize_snapshot(workspace_root: Path, profile: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    records = build_inventory_records(workspace_root, profile)
    store = snapshot_dir(snapshot_id)
    store.mkdir(parents=True, exist_ok=False)
    for rec in records:
        src = workspace_root / Path(rec["relative_path"])
        if rec["file_kind"] == "absent":
            continue
        if src.is_symlink():
            raise ValueError("snapshots require regular files; exclude links or materialize them first")
        dst = store / rec["relative_path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    binding = workspace_files_inventory_sha256(records)
    copied = workspace_files_inventory_sha256(build_inventory_records(store, profile))
    if copied != binding:
        raise ValueError("snapshot bytes do not match source inventory")
    meta = {"snapshot_id": snapshot_id, "snapshot_binding_sha256": binding, "file_count": len(records)}
    return meta


def snapshot_dir(snapshot_id: str) -> Path:
    manifest = load_json_ref("config/uve/uve_execution_binding_manifest.json")
    root = (pipeline_root() / manifest["snapshot_store_relative"]).resolve()
    path = (root / snapshot_id).resolve()
    if path.parent != root:
        raise ValueError("snapshot id must be a single directory name")
    return path
