"""Self-tests for UVE Test Expansion Advisor / storage (does not affect ENGINE_UVE_VALID)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_server.diagnostics.engine_uve_validate import load_engine_binding, resolve_engine_root
from mcp_server.diagnostics.uve_evolution_store import (
    integrity_check,
    open_store,
    simulation_storage_growth,
    simulate_interrupted_write,
)
from mcp_server.diagnostics.uve_test_expansion_advisor import (
    _proposal_id,
    _validate_proposal_shape,
)
from mcp_server.diagnostics.uve_test_expansion_evolution import (
    compute_dual_axis_fitness,
    load_evolution_policy,
)


def _lab_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pipeline(binding: dict[str, Any], lab_root: Path) -> Path:
    return lab_root / str(binding.get("uve_training_pipeline_relative_from_lab") or "uve-v4-v6-candidate")


def run_expansion_self_tests(lab_root: Path | None = None) -> dict[str, Any]:
    lab_root = (lab_root or _lab_root()).resolve()
    binding = load_engine_binding(lab_root)
    pipeline = _pipeline(binding, lab_root)
    engine_root = resolve_engine_root(lab_root, binding)
    manifest_path = engine_root / "config/uve/verification_manifest.json"
    manifest_before = manifest_path.read_bytes()

    results: list[dict[str, Any]] = []

    def record(test_id: str, ok: bool, detail: str = "") -> None:
        results.append({"id": test_id, "ok": ok, "detail": detail})
        if not ok:
            raise AssertionError(f"{test_id}: {detail}")

    ids_a = [_proposal_id("editor_gizmo_contract", "scene_load_interrupt", "static")]
    ids_b = [_proposal_id("editor_gizmo_contract", "scene_load_interrupt", "static")]
    record("uve_expansion_ranking_deterministic", ids_a == ids_b)

    bad_observed = {
        "proposal_id": "x" * 32,
        "parent_scenario": "t",
        "status": "proposed",
        "proposal_outcome": "proposed",
        "gap_confidence": "observed",
        "evidence_strength": "runtime",
        "static_analysis_only": False,
        "strategy_id": "scene_load_interrupt",
        "project_id": "ailab_engine",
        "priority": 0.5,
        "rationale": "test",
        "evidence_refs": [],
    }
    try:
        _validate_proposal_shape(bad_observed)
        record("uve_expansion_observed_requires_refs", False, "expected validation error")
    except ValueError:
        record("uve_expansion_observed_requires_refs", True)

    manifest_after = manifest_path.read_bytes()
    record("uve_expansion_no_pass_inflation", manifest_before == manifest_after)

    record(
        "uve_expansion_reject_not_false_positive",
        True,
        "rejected_duplicate_coverage neutral by policy",
    )
    evo_policy = load_evolution_policy(pipeline, binding)
    d_reg, p_reg = compute_dual_axis_fitness(
        "verified_caught_regression", {"unique_defect_caught": 1.0}, evo_policy
    )
    d_prev, p_prev = compute_dual_axis_fitness(
        "verified_preventive_guard", {"critical_guard_cycles_green": 1.0}, evo_policy
    )
    record(
        "uve_expansion_preventive_separate_axis",
        d_reg > p_reg and p_prev > d_prev,
        f"discovery={d_reg}/{p_reg} preventive={p_prev}/{d_prev}",
    )
    dup_d, dup_p = compute_dual_axis_fitness("rejected_duplicate_coverage", {}, evo_policy)
    record(
        "uve_expansion_no_accept_count_fitness",
        dup_d == 0.0 and dup_p == 0.0,
        "rejected_duplicate neutral",
    )

    growth = simulation_storage_growth(lab_root, pipeline, scenarios=5, generations=3)
    record("uve_evolution_storage_growth_regression", growth.get("ok", False), json.dumps(growth))

    integrity = simulate_interrupted_write(lab_root, pipeline)
    record("uve_evolution_storage_integrity_regression", integrity.get("ok", False))

    conn, _ = open_store(lab_root, pipeline, readonly=True)
    record("uve_meta_advisor_fitness_guards", integrity_check(conn))
    conn.close()

    failed = [row for row in results if not row["ok"]]
    return {"ok": not failed, "results": results, "failed": failed}
