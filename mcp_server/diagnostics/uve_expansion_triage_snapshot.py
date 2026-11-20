"""Read-only expansion advisor / evolution snapshot for triage session reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_server.diagnostics.engine_uve_validate import load_engine_binding, resolve_engine_root
from mcp_server.diagnostics.uve_evolution_store import open_store
from mcp_server.diagnostics.uve_test_expansion_evolution import _pipeline_root, load_rank_overlay, overlay_path


def load_expansion_triage_snapshot(lab_root: Path) -> dict[str, Any]:
    lab_root = lab_root.resolve()
    binding = load_engine_binding(lab_root)
    engine_root = resolve_engine_root(lab_root, binding)
    pipeline = _pipeline_root(lab_root, binding)

    overlay = load_rank_overlay(engine_root, binding)
    bias = overlay.get("strategy_rank_bias") or {}
    top_strategies = sorted(
        ((str(k), float(v)) for k, v in bias.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:8]

    from mcp_server.diagnostics.uve_triage_store import BOOTSTRAP, load_config, open_store as open_index
    from mcp_server.diagnostics.uve_triage_migration import primary_advisor_snapshot

    index_config = load_config(lab_root) if (lab_root / BOOTSTRAP).is_file() else None
    if index_config and index_config.policy("adapter_ref")["read_primary"]:
        with open_index(index_config, readonly=True) as indexed:
            snapshot = primary_advisor_snapshot(indexed, index_config)
        last_generation = None
        if snapshot["last_generation"] is not None:
            last_generation = {"generation": snapshot["last_generation"],
                               "summary": json.loads(snapshot["generation_summary_json"])}
        return {
            "schema_version": "ailab.uve.expansion_triage_snapshot.v1",
            "database": index_config.spec["database_relative_from_lab"],
            "overlay_path": str(overlay_path(engine_root, binding).relative_to(engine_root)).replace("\\", "/"),
            "proposal_count": snapshot["proposal_count"],
            "observed_proposal_count": snapshot["observed_proposal_count"],
            "fitness_record_count": snapshot["fitness_record_count"],
            "strategy_rank_bias_top": [{"strategy_id": sid, "bias": score} for sid, score in top_strategies],
            "last_evolution_generation": last_generation,
            "overlay_generation": overlay.get("generation"),
        }

    conn, db_path = open_store(lab_root, pipeline, readonly=True)
    proposal_count = int(conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0])
    fitness_count = int(conn.execute("SELECT COUNT(*) FROM fitness_records").fetchone()[0])
    gen_row = conn.execute(
        "SELECT generation, summary_json FROM generation_summaries ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    observed_count = 0
    for (_proposal_id, record_json) in conn.execute(
        "SELECT proposal_id, record_json FROM proposals"
    ).fetchall():
        record = json.loads(record_json)
        if record.get("gap_confidence") == "observed":
            observed_count += 1
    conn.close()

    last_generation: dict[str, Any] | None = None
    if gen_row:
        last_generation = {
            "generation": int(gen_row[0]),
            "summary": json.loads(gen_row[1]),
        }

    return {
        "schema_version": "ailab.uve.expansion_triage_snapshot.v1",
        "database": str(db_path.relative_to(lab_root)).replace("\\", "/"),
        "overlay_path": str(overlay_path(engine_root, binding).relative_to(engine_root)).replace(
            "\\", "/"
        ),
        "proposal_count": proposal_count,
        "observed_proposal_count": observed_count,
        "fitness_record_count": fitness_count,
        "strategy_rank_bias_top": [{"strategy_id": sid, "bias": score} for sid, score in top_strategies],
        "last_evolution_generation": last_generation,
        "overlay_generation": overlay.get("generation"),
    }
