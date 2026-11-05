from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_training.manifest_loader import ManifestError, load_json_ref
from agent_training.paths import pipeline_root


def run_verifier(verifier_id: str, task_registry_ref: str, seed: int, genome_bonus: float = 0.0) -> bool:
    registry = load_json_ref(task_registry_ref)
    spec = registry["verifiers"].get(verifier_id)
    if not spec:
        raise KeyError(verifier_id)
    kind = spec["kind"]
    if kind == "synthetic_build":
        return (seed % 7) != 0
    if kind == "synthetic_tests":
        ratio = 1.0 if (seed % 5) != 0 else 0.5
        return ratio >= float(spec.get("min_pass_ratio", 1.0)) and genome_bonus >= 0.0
    if kind == "synthetic_invariant":
        return bool(spec.get("expected_flag", True)) and (seed % 11) != 0
    if kind == "uve_gate_receipt_ref":
        rel = spec.get("gate_receipt_relpath")
        if not rel:
            raise ManifestError("uve_gate_receipt_ref requires gate_receipt_relpath")
        gate_path = pipeline_root() / rel
        if not gate_path.is_file():
            return False
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        required = spec.get("required_decision", "approved")
        if gate.get("decision") != required:
            return False
        from agent_training.uve.gate_evaluator import validate_stored_gate
        return validate_stored_gate(gate, expected_decision=required)
    raise ValueError(f"unknown verifier kind: {kind}")


def evaluate_task(task_id: str, seed: int, task_registry_ref: str, genome_bonus: float) -> dict[str, Any]:
    registry = load_json_ref(task_registry_ref)
    task = next(t for t in registry["procedural_tasks"] if t["task_id"] == task_id)
    results: dict[str, bool] = {}
    for vid in task["verifier_ids"]:
        results[vid] = run_verifier(vid, task_registry_ref, seed + hash(vid) % 1000, genome_bonus)
    return {"task_id": task_id, "seed": seed, "verifier_results": results}
