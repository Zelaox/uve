#!/usr/bin/env python3
"""Compaction hook for UVE expansion SQLite (policy-driven; preserves evidence holds)."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.engine_uve_validate import load_engine_binding
from mcp_server.diagnostics.uve_evolution_store import (
    integrity_check,
    load_storage_policy,
    open_store,
)


def _load_hold_policy(pipeline: Path) -> dict:
    storage = load_storage_policy(pipeline)
    ref = str(storage.get("evidence_hold_policy_ref") or "config/uve/uve_evolution_evidence_hold_policy.json")
    return json.loads((pipeline / ref).read_text(encoding="utf-8"))


def _pinned_proposal_ids(conn, pin_outcomes: list[str]) -> set[str]:
    pinned: set[str] = set()
    for outcome in pin_outcomes:
        rows = conn.execute(
            "SELECT proposal_id FROM proposals WHERE proposal_outcome = ?", (outcome,)
        ).fetchall()
        pinned.update(str(row[0]) for row in rows)
    return pinned


def main() -> int:
    binding = load_engine_binding(ROOT)
    pipeline = ROOT / str(binding.get("uve_training_pipeline_relative_from_lab") or "uve-v4-v6-candidate")
    storage = load_storage_policy(pipeline)
    hold_policy = _load_hold_policy(pipeline)
    conn, db_path = open_store(ROOT, pipeline)
    if not integrity_check(conn):
        print("COMPACTION_ABORT integrity_check failed", file=sys.stderr)
        conn.close()
        return 1
    pin_outcomes = [str(x) for x in (hold_policy.get("pin_outcomes") or [])]
    pinned = _pinned_proposal_ids(conn, pin_outcomes)
    retention = int(storage.get("evaluation_detail_retention_generations") or 20)
    max_gen_row = conn.execute("SELECT MAX(generation) FROM proposal_evaluations").fetchone()
    max_gen = int(max_gen_row[0]) if max_gen_row and max_gen_row[0] is not None else 0
    prune_before = max_gen - retention
    rows_pruned = 0
    if prune_before > 0:
        candidates = conn.execute(
            "SELECT evaluation_id, proposal_id FROM proposal_evaluations WHERE generation < ?",
            (prune_before,),
        ).fetchall()
        for evaluation_id, proposal_id in candidates:
            if str(proposal_id) in pinned:
                continue
            conn.execute("DELETE FROM proposal_evaluations WHERE evaluation_id = ?", (evaluation_id,))
            rows_pruned += 1
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO compaction_runs (run_id, started_at, finished_at, rows_pruned, "
        "holds_preserved, integrity_check_ok, aborted, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            rows_pruned,
            len(pinned),
            1,
            0,
            json.dumps({"mode": "evaluation_prune", "retention_generations": retention}),
        ),
    )
    conn.commit()
    conn.close()
    print(
        json.dumps(
            {
                "ok": True,
                "database": str(db_path),
                "compaction_run_id": run_id,
                "rows_pruned": rows_pruned,
                "holds_preserved": len(pinned),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
