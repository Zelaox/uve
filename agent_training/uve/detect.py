from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_training.uve.identity import build_inventory_records, workspace_files_inventory_sha256


def inventory_diff(
    workspace_root: Path,
    profile: dict,
    previous_records: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    current = build_inventory_records(workspace_root, profile)
    current_hash = workspace_files_inventory_sha256(current)
    if previous_records is None:
        return {"changed": True, "workspace_files_inventory_sha256": current_hash, "records": current}
    prev_hash = workspace_files_inventory_sha256(previous_records)
    prev_paths = {r["relative_path"] for r in previous_records}
    cur_paths = {r["relative_path"] for r in current}
    previous_by_path = {r["relative_path"]: r for r in previous_records}
    current_by_path = {r["relative_path"]: r for r in current}
    return {
        "changed": current_hash != prev_hash,
        "workspace_files_inventory_sha256": current_hash,
        "added_paths": sorted(cur_paths - prev_paths),
        "removed_paths": sorted(prev_paths - cur_paths),
        "modified_paths": sorted(p for p in prev_paths & cur_paths if previous_by_path[p] != current_by_path[p]),
        "records": current,
    }
