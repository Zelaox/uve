"""Strategy-level fitness and rank overlay (NEAT-inspired EA — not neural NEAT)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp_server.diagnostics.engine_uve_validate import load_engine_binding, resolve_engine_root
from mcp_server.diagnostics.uve_evolution_store import open_store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pipeline_root(lab_root: Path, binding: dict[str, Any]) -> Path:
    return (lab_root / binding["uve_training_pipeline_relative_from_lab"]).resolve()


def load_evolution_policy(pipeline_root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    path = pipeline_root / binding["expansion_evolution_policy_relative_in_pipeline"]
    return json.loads(path.read_text(encoding="utf-8"))


def overlay_path(engine_root: Path, binding: dict[str, Any]) -> Path:
    return engine_root / binding["expansion_strategy_rank_overlay_relative_from_engine"]


def load_rank_overlay(engine_root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    path = overlay_path(engine_root, binding)
    if not path.is_file():
        return {"schema_version": "ailab.uve.strategy_rank_overlay.v1", "strategy_rank_bias": {}}
    overlay = json.loads(path.read_text(encoding="utf-8"))
    if "evaluation_ids" not in overlay:
        overlay["strategy_rank_bias"] = {}
    return overlay


def compute_dual_axis_fitness(
    proposal_outcome: str,
    components: Optional[dict[str, Any]],
    evolution_policy: dict[str, Any],
) -> tuple[float, float]:
    signals = evolution_policy["signal_weights"]
    components = components or {}
    discovery = 0.0
    preventive = 0.0
    if proposal_outcome == "verified_caught_regression":
        discovery += float(signals["unique_defect_caught"]) * float(
            components.get("unique_defect_caught", 1.0)
        )
        discovery += float(signals["misleading_pass_prevented"]) * float(
            components.get("misleading_pass_prevented", 0.0)
        )
    elif proposal_outcome == "verified_preventive_guard":
        preventive += float(signals["critical_guard_cycles_green"]) * float(
            components.get("critical_guard_cycles_green", 1.0)
        )
    elif proposal_outcome == "verified_no_defect_found":
        preventive += float(signals["critical_guard_cycles_green"]) * float(
            components.get("critical_guard_cycles_green", 0.0)
        )
    elif proposal_outcome == "rejected_true_false_alarm":
        discovery += float(signals["true_false_positive"])
    elif proposal_outcome in {
        "rejected_duplicate_coverage",
        "rejected_too_costly",
        "rejected_deprioritized",
        "proposed",
    }:
        pass
    cost = float(components.get("cost_ms", 0.0))
    discovery += float(signals["cost_ms"]) * cost
    return discovery, preventive


def write_evaluation_fitness(
    conn,
    *,
    evaluation: dict[str, Any],
    evolution_policy: dict[str, Any],
    strategy_id: str,
    proposal_id: str,
    generation: int,
    proposal_outcome: str,
    fitness_components: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    discovery, preventive = compute_dual_axis_fitness(
        proposal_outcome, fitness_components, evolution_policy
    )
    record = {
        "schema_version": "ailab.uve.strategy_fitness.v1",
        "fitness_id": hashlib.sha256(evaluation["evaluation_id"].encode()).hexdigest(),
        "evaluation_id": evaluation["evaluation_id"],
        "strategy_id": strategy_id,
        "proposal_id": proposal_id,
        "generation": generation,
        "proposal_outcome": proposal_outcome,
        "defect_discovery_fitness": discovery,
        "preventive_coverage_value": preventive,
        "fitness_components": fitness_components or {},
        "evidence_refs": [evaluation["gate_ref"], evaluation["probe_run_id"]],
    }
    conn.execute(
        """
        INSERT INTO fitness_records
        (fitness_id, strategy_id, proposal_id, generation,
         defect_discovery_fitness, preventive_coverage_value, record_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["fitness_id"],
            strategy_id,
            proposal_id,
            generation,
            discovery,
            preventive,
            json.dumps(record, ensure_ascii=False),
        ),
    )
    return record


def run_evolution_generation(lab_root: Path, *, generation: Optional[int] = None) -> dict[str, Any]:
    lab_root = lab_root.resolve()
    binding = load_engine_binding(lab_root)
    pipeline = _pipeline_root(lab_root, binding)
    engine_root = resolve_engine_root(lab_root, binding)
    evolution_policy = load_evolution_policy(pipeline, binding)
    weights = evolution_policy["fitness_weights"]
    w_d = float(weights["defect_discovery_w"])
    w_p = float(weights["preventive_coverage_w"])

    conn, db_path = open_store(lab_root, pipeline)
    try:
        conn.execute("BEGIN IMMEDIATE")
        return _publish_generation(conn, db_path, overlay_path(engine_root, binding),
                                   generation, evolution_policy["fitness_history_window"], w_d, w_p)
    finally:
        conn.close()


def _publish_generation(conn, db_path, path, generation, history_window, w_d, w_p):
    rows = conn.execute(
        """SELECT f.strategy_id, f.defect_discovery_fitness, f.preventive_coverage_value, e.evaluation_id
           FROM fitness_records f JOIN proposal_evaluations e
           ON e.evaluation_id=json_extract(f.record_json, '$.evaluation_id')
           AND e.proposal_id=f.proposal_id
           ORDER BY e.rowid DESC LIMIT ?""", (history_window,),
    ).fetchall()
    aggregates: dict[str, dict[str, float]] = {}
    for strategy_id, discovery, preventive, _ in rows:
        bucket = aggregates.setdefault(
            str(strategy_id), {"discovery": 0.0, "preventive": 0.0, "count": 0.0}
        )
        bucket["discovery"] += float(discovery)
        bucket["preventive"] += float(preventive)
        bucket["count"] += 1.0

    bias: dict[str, float] = {}
    for strategy_id, bucket in aggregates.items():
        count = max(bucket["count"], 1.0)
        combined = w_d * (bucket["discovery"] / count) + w_p * (bucket["preventive"] / count)
        bias[strategy_id] = round(combined, 6)

    previous = conn.execute("SELECT generation,summary_json FROM generation_summaries ORDER BY generation DESC LIMIT 1").fetchone()
    evaluation_ids = [row[3] for row in rows]
    if generation is None and previous and json.loads(previous[1]).get("evaluation_ids") == evaluation_ids:
        generation = previous[0]
    next_gen = generation if generation is not None else previous[0] + 1 if previous else 1
    summary = {
        "generation": next_gen,
        "strategy_count": len(bias),
        "fitness_row_count": len(rows),
        "evaluation_ids": evaluation_ids,
        "top_strategies": sorted(bias.items(), key=lambda item: item[1], reverse=True)[:5],
        "updated_at": _utc_now(),
    }
    conn.execute(
        "INSERT OR REPLACE INTO generation_summaries (generation, summary_json) VALUES (?, ?)",
        (next_gen, json.dumps(summary, ensure_ascii=False)),
    )
    overlay = {
        "schema_version": "ailab.uve.strategy_rank_overlay.v1",
        "updated_at": _utc_now(),
        "generation": next_gen,
        "evaluation_ids": evaluation_ids,
        "strategy_rank_bias": bias,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    conn.commit()

    return {
        "ok": True,
        "generation": next_gen,
        "database": str(db_path),
        "overlay_path": str(path),
        "strategy_rank_bias": bias,
        "summary": summary,
    }
