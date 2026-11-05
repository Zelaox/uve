from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_training.manifest_loader import load_json_ref
from agent_training.uve.detect import inventory_diff
from agent_training.uve.identity import matches_glob


def plan_probes_for_changes(
    profile: dict[str, Any],
    workspace_root: Path,
    previous_records: list[dict[str, Any]] | None,
) -> list[str]:
    diff = inventory_diff(workspace_root, profile, previous_records)
    rules_ref = profile.get("change_class_rules_ref", "config/uve/uve_change_class_rules.json")
    rules_doc = load_json_ref(rules_ref)
    changed_paths = set(diff.get("added_paths", []))
    changed_paths.update(diff.get("removed_paths", []))
    changed_paths.update(diff.get("modified_paths", []))
    if previous_records is None:
        changed_paths.update(r["relative_path"] for r in diff["records"])
    if not diff.get("changed"):
        return list(profile.get("verification_probe_ids", ["engine_self_check"]))

    planned: set[str] = set()
    for rule in rules_doc["rules"]:
        globs = rule["path_globs"]
        probe_ids = rule["probe_ids"]
        for path in changed_paths:
            p = Path(path)
            if any(matches_glob(path, g) for g in globs):
                planned.update(probe_ids)
    if not planned:
        for rule in rules_doc["rules"]:
            if rule["rule_id"] == "default":
                planned.update(rule["probe_ids"])
    return sorted(planned)
