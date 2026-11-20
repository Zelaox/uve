"""Manifest-driven UVE Test Expansion Advisor (advisory only — no ENGINE_UVE_VALID inflation)."""

from __future__ import annotations

import hashlib
import json
import random
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp_server.diagnostics.engine_uve_validate import (
    _resolve_scenario_ids,
    load_engine_binding,
    resolve_engine_root,
)
from mcp_server.diagnostics.uve_evolution_store import open_store, upsert_proposal
from mcp_server.diagnostics.uve_test_expansion_evolution import _pipeline_root, load_evolution_policy, load_rank_overlay
from mcp_server.diagnostics.uve_triage_store import json_text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_strategy_variant(
    strategy: dict[str, Any],
    rng: random.Random,
    *,
    mutation_enabled: bool,
) -> tuple[str, str]:
    variants = strategy.get("variants") or []
    if not mutation_enabled or not variants:
        base = str(strategy.get("gap_heuristic") or "static")
        return base, base
    pick = variants[int(rng.random() * len(variants)) % len(variants)]
    suffix = str(pick.get("gap_heuristic_suffix") or pick.get("variant_id") or "variant")
    base = str(strategy.get("gap_heuristic") or "static")
    return f"{base}:{suffix}", suffix


def _sync_proposal_index(engine_root: Path, binding: dict[str, Any], conn) -> None:
    index_path = engine_root / binding["expansion_proposal_index_relative_from_engine"]
    rows = conn.execute("SELECT proposal_id, record_json FROM proposals").fetchall()
    proposals: dict[str, Any] = {}
    for proposal_id, record_json in rows:
        record = json.loads(record_json)
        proposals[str(proposal_id)] = {
            "parent_scenario": record.get("parent_scenario"),
            "strategy_id": record.get("strategy_id"),
            "status": record.get("status"),
            "gap_confidence": record.get("gap_confidence"),
        }
    payload = {
        "schema_version": "ailab.uve.proposal_index.v1",
        "proposals": proposals,
    }
    if index_path.is_file() and _load_json(index_path).get("proposals") == proposals:
        return
    payload["updated_at"] = _utc_now()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _proposal_id(parent_scenario: str, strategy_id: str, gap_fingerprint: str) -> str:
    raw = f"{parent_scenario}|{strategy_id}|{gap_fingerprint}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _validate_proposal_shape(record: dict[str, Any]) -> None:
    required = (
        "proposal_id",
        "parent_scenario",
        "status",
        "proposal_outcome",
        "gap_confidence",
        "evidence_strength",
        "static_analysis_only",
        "strategy_id",
        "project_id",
        "priority",
        "rationale",
    )
    for key in required:
        if key not in record:
            raise ValueError(f"proposal missing required field: {key}")
    if record["gap_confidence"] == "observed":
        refs = record.get("evidence_refs") or []
        if not refs:
            raise ValueError("observed gap requires evidence_refs")


def _triage_evidence(lab_root: Path, binding: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    from mcp_server.diagnostics.uve_triage_sources import discover
    from mcp_server.diagnostics.uve_triage_store import load_config

    config = load_config(lab_root)
    policy = _load_json(lab_root / binding["engine_triage_policy_relative_from_lab"])
    spec = config.policy("ingest_ref")
    newest = {}
    selected_types = [route["source_type"] for route in spec["routes"]
                      if route.get("project_binding") == "engine" and route["normalizer"] in ("runtime", "session")]
    for source in discover(config, spec, selected_types):
        route = source.route
        stamp = source.path.stat().st_mtime_ns
        if stamp > newest.get(route["source_type"], (0, ""))[0]:
            newest[route["source_type"]] = (stamp, source.relative)
    return policy["triage_to_expansion_strategy"], [reference for _, reference in newest.values()]


def _reported_codes(lab_root: Path, references: list[str]) -> list[str]:
    from mcp_server.diagnostics.uve_triage_ingest import _records

    return list(dict.fromkeys(
        code for reference in references if reference.endswith(".json")
        for finding in _records(_load_json(lab_root / reference)) if isinstance(finding, dict)
        if (code := finding.get("diagnostic_code") or finding.get("finding_code") or finding.get("code"))
    ))


def _rank_strategies(
    strategies: list[dict[str, Any]],
    hints: list[str],
    observed_strategy: Optional[str],
    rng: random.Random,
    exploration_fraction: float,
    untried: set[str],
    tried: set[str],
    rank_bias: dict[str, float],
    exploration_boost_untried: float,
) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in strategies:
        sid = str(item["strategy_id"])
        score = float(item.get("catalog_priority") or 0.5)
        score += float(rank_bias.get(sid, 0.0))
        if sid in hints:
            score += 0.3
        if observed_strategy and sid == observed_strategy:
            score += 0.5
        if sid in untried and sid not in tried:
            score += exploration_fraction + exploration_boost_untried
        scored.append((score + rng.random() * 0.001, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def run_test_expansion_advisory(
    lab_root: Path,
    *,
    triage_codes: Optional[list[str]] = None,
    generation: int | None = None,
) -> dict[str, Any]:
    lab_root = lab_root.resolve()
    binding = load_engine_binding(lab_root)
    engine_root = resolve_engine_root(lab_root, binding)
    pipeline = _pipeline_root(lab_root, binding)
    policy_ref = binding["expansion_advisor_policy_relative_in_pipeline"]
    advisor_policy = _load_json(pipeline / policy_ref)
    catalog = _load_json(pipeline / str(advisor_policy["strategy_catalog_ref"]))
    hints_path = engine_root / str(
        binding.get("expansion_scenario_hints_relative_from_engine")
        or "config/uve/expansion/scenario_expansion_hints.json"
    )
    scenario_hints = (_load_json(hints_path).get("hints") or {}) if hints_path.is_file() else {}

    manifest = _load_json(
        engine_root / str(binding.get("verification_manifest_relative_from_engine") or "config/uve/verification_manifest.json")
    )
    scenarios = _resolve_scenario_ids(engine_root, manifest["engine_contracts"])
    max_run = int(advisor_policy.get("max_scenarios_per_run") or 0)
    if max_run > 0:
        scenarios = scenarios[:max_run]

    triage_map, evidence_refs = _triage_evidence(lab_root, binding)
    observed_codes = {ref: _reported_codes(lab_root, [ref]) for ref in evidence_refs}
    available_codes = list(dict.fromkeys(code for codes in observed_codes.values() for code in codes))
    triage_codes = available_codes if triage_codes is None else [code for code in triage_codes if code in available_codes]
    observed_strategy: Optional[str] = None
    for code in triage_codes:
        if code in triage_map:
            observed_strategy = triage_map[code]
            evidence_refs = [ref for ref, codes in observed_codes.items() if code in codes]
            break

    overlay = load_rank_overlay(engine_root, binding)
    generation = generation if generation is not None else int(overlay.get("generation", 1))
    rank_bias = overlay.get("strategy_rank_bias") or {}
    if not isinstance(rank_bias, dict):
        rank_bias = {}
    evolution_cfg = load_evolution_policy(pipeline, binding)
    exploration_boost_untried = float(evolution_cfg["exploration_boost_untried"])
    mutation_enabled = evolution_cfg["mutation_enabled"]

    rng = random.Random(int(advisor_policy.get("advisor_seed") or 1) + generation)
    strategies = list(catalog.get("strategies") or [])
    min_p = int(advisor_policy.get("proposals_per_scenario_min") or 2)
    max_p = int(advisor_policy.get("proposals_per_scenario_max") or 3)
    exploration = float(advisor_policy.get("exploration_slot_fraction") or 0.25)
    conn, db_path = open_store(lab_root, pipeline)
    tried = {row[0] for row in conn.execute(
        """SELECT DISTINCT p.strategy_id FROM proposals p JOIN proposal_evaluations e USING(proposal_id)""")}
    untried = {str(s["strategy_id"]) for s in strategies} - tried
    all_proposals: list[dict[str, Any]] = []
    changed_proposals: list[dict[str, Any]] = []

    for parent in scenarios:
        hints = [str(h) for h in (scenario_hints.get(parent) or [])]
        ranked = _rank_strategies(
            strategies,
            hints,
            observed_strategy,
            rng,
            exploration,
            untried,
            tried,
            rank_bias,
            exploration_boost_untried,
        )
        count = min(max_p, max(min_p, len(ranked)))
        picked = ranked[:count]
        for rank, strategy in enumerate(picked):
            sid = str(strategy["strategy_id"])
            tried.add(sid)
            gap_fingerprint, variant = _apply_strategy_variant(
                strategy, rng, mutation_enabled=mutation_enabled and generation > 1
            )
            is_observed = observed_strategy == sid and bool(evidence_refs)
            record: dict[str, Any] = {
                "proposal_id": _proposal_id(parent, sid, gap_fingerprint),
                "parent_scenario": parent,
                "status": "proposed",
                "proposal_outcome": "proposed",
                "strategy_id": sid,
                "project_id": str(advisor_policy.get("project_id") or binding.get("project_id")),
                "gap_confidence": "observed" if is_observed else "suspected",
                "evidence_strength": "triage" if is_observed else "static",
                "static_analysis_only": not is_observed,
                "priority": round(1.0 - rank * 0.1, 4),
                "rationale": str(strategy.get("description") or sid),
                "missing_coverage": [gap_fingerprint],
                "proposed_procedure": [
                    f"ailab_test_runner --verify-scenario {parent} --seed {advisor_policy.get('advisor_seed', 1)}",
                    f"extend with strategy {sid}",
                ],
                "expected_assertions": ["VERIFY_OK"],
                "evidence_refs": evidence_refs if is_observed else [],
                "generation": generation,
                "variant_id": variant,
                "created_at": _utc_now(),
            }
            _validate_proposal_shape(record)
            if upsert_proposal(conn, record):
                changed_proposals.append(record)
            saved = conn.execute("SELECT record_json FROM proposals WHERE proposal_id=?", (record["proposal_id"],)).fetchone()
            all_proposals.append(json.loads(saved[0]))
    conn.commit()
    if changed_proposals:
        _sync_proposal_index(engine_root, binding, conn)
    conn.close()

    jsonl_path: Optional[Path] = None
    if advisor_policy["write_jsonl_export"] and changed_proposals:
        rel = advisor_policy["jsonl_relative_from_lab"]
        jsonl_path = lab_root / rel
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as handle:
            for record in changed_proposals:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    from mcp_server.diagnostics.uve_triage_adapter import index_sidecar

    index_result = index_sidecar(lab_root, "advisor_mirror") if changed_proposals else {"status": "unchanged"}
    return {
        "ok": True,
        "proposal_count": len(all_proposals),
        "observability_index": index_result,
        "scenario_count": len(scenarios),
        "database": str(db_path),
        "jsonl": str(jsonl_path) if jsonl_path else None,
        "proposals_sample": all_proposals[:3],
    }


def transition_proposal_status(
    lab_root: Path,
    proposal_id: str,
    new_status: str,
    *,
    proposal_outcome: Optional[str] = None,
    actor: str | None = None,
) -> dict[str, Any]:
    from mcp_server.diagnostics.uve_expansion_feedback import evaluate_proposal, load_feedback
    from mcp_server.diagnostics.uve_test_expansion_evolution import write_evaluation_fitness

    binding = load_engine_binding(lab_root)
    pipeline = _pipeline_root(lab_root, binding)
    policy = load_feedback(lab_root, binding)
    if new_status == "verified" and (not actor or proposal_outcome):
        raise ValueError("Verification requires an actor and derives its outcome from UVE")
    if new_status == "rejected" and proposal_outcome not in policy["rejection_outcomes"]:
        raise ValueError("Rejection requires a configured rejection outcome")
    if new_status not in {"accepted", "implemented", "rejected", "verified"}:
        raise ValueError("Unknown proposal lifecycle status")

    def load_record(conn):
        row = conn.execute("SELECT record_json FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown proposal_id: {proposal_id}")
        return json.loads(row[0])

    conn, _ = open_store(lab_root, pipeline, readonly=True)
    with closing(conn):
        original = load_record(conn)
    if original["status"] in {"rejected", "verified"} and original["status"] != new_status:
        raise ValueError("A terminal proposal cannot be reset")
    evaluation = evaluate_proposal(lab_root, binding, original, actor=actor) if new_status == "verified" else None
    conn, _ = open_store(lab_root, pipeline)
    with closing(conn), conn:
        conn.execute("BEGIN IMMEDIATE")
        record = load_record(conn)
        if record != original:
            raise ValueError("Proposal changed during evaluation. Retry with the current proposal.")
        record["status"] = new_status
        if evaluation:
            outcome = evaluation["proposal_outcome"]
            generation = record["generation"]
            evaluation["variant_id"] = record.get("variant_id")
            inserted = conn.execute(
                "INSERT OR IGNORE INTO proposal_evaluations VALUES (?,?,?,?,?)",
                (evaluation["evaluation_id"], proposal_id, generation, evaluation["inputs_hash"], json_text(evaluation)),
            ).rowcount
            if inserted:
                write_evaluation_fitness(
                    conn, evaluation=evaluation, evolution_policy=load_evolution_policy(pipeline, binding),
                    strategy_id=record["strategy_id"], proposal_id=proposal_id, generation=generation,
                    proposal_outcome=outcome,
                )
            else:
                stored = conn.execute("SELECT record_json FROM proposal_evaluations WHERE evaluation_id=?",
                                      (evaluation["evaluation_id"],)).fetchone()
                evaluation = json.loads(stored[0])
            record.update(proposal_outcome=outcome, evaluation_id=evaluation["evaluation_id"])
        elif proposal_outcome:
            record["proposal_outcome"] = proposal_outcome
        if record != original:
            upsert_proposal(conn, record, transition=True)
    from mcp_server.diagnostics.uve_triage_adapter import index_sidecar

    if record != original:
        conn, _ = open_store(lab_root, pipeline, readonly=True)
        with closing(conn):
            _sync_proposal_index(resolve_engine_root(lab_root, binding), binding, conn)
    if evaluation or record != original:
        index_sidecar(lab_root, "advisor_mirror")
    return record
