from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_training.uve.bundle import resolve_log_dir, load_engine_manifest, load_manifest_bundle
from agent_training.manifest_loader import load_json_ref
from agent_training.uve.engine_state import read_engine_state


def collect_uve_status() -> dict[str, Any]:
    log_dir = resolve_log_dir()
    probes_dir = log_dir / "probes"
    gates_dir = log_dir / "gates"
    latest_probe = _latest_json(probes_dir)
    latest_gate = _latest_json(gates_dir)
    engine = read_engine_state()
    runtime = None
    runtime_ref = load_engine_manifest().get("runtime_manifest_ref")
    if runtime_ref:
        runtime_spec = load_json_ref(runtime_ref)
        runtime_file = log_dir / runtime_spec["status_relative"]
        if runtime_file.is_file():
            try:
                from agent_training.uve.status_freshness import current_runtime_status
                runtime = current_runtime_status(json.loads(runtime_file.read_text(encoding="utf-8")), runtime_spec)
            except (OSError, ValueError):
                runtime = {"state": "not_verified", "reason": "runtime status unavailable"}
    issues_dir = log_dir / "issues"
    from agent_training.uve.flow_status import collect_deep_status

    try:
        deep_flows = collect_deep_status(load_manifest_bundle()["manifest_bundle_sha256"])
    except Exception:
        deep_flows = []
    issue_count = 0
    if issues_dir.is_dir():
        for path in issues_dir.glob("*.json"):
            try:
                issue = json.loads(path.read_text(encoding="utf-8"))
                if "resolution" in issue and issue["resolution"].get("status") != "closed":
                    issue_count += 1
            except (OSError, ValueError):
                continue
    return {
        "engine": engine,
        "runtime": runtime,
        "latest_probe": latest_probe,
        "latest_gate": latest_gate,
        "deep_flows": deep_flows,
        "open_issue_artifacts": issue_count,
        "log_dir": str(log_dir),
    }


def _latest_json(directory: Path) -> dict[str, Any] | None:
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    for path in files:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None
