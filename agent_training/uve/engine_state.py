from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_training.manifest_loader import load_json_ref, pipeline_root


def engine_state_path() -> Path:
    return pipeline_root() / "config/uve/uve_engine_state.json"


def read_engine_state() -> dict[str, Any]:
    path = engine_state_path()
    if not path.is_file():
        return {"engine_degraded": True, "reason": "missing engine state file"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"engine_degraded": True, "reason": f"unreadable engine state: {exc}"}


def write_engine_state(*, engine_degraded: bool, probe_run_id: str | None) -> dict[str, Any]:
    doc = {
        "schema_version": "uve_engine_state.v1",
        "engine_degraded": engine_degraded,
        "last_self_check_probe_run_id": probe_run_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = engine_state_path()
    from agent_training.uve.bundle import write_json_atomic
    write_json_atomic(path, doc)
    return doc
