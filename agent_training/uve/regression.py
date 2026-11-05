from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref, pipeline_root
from agent_training.uve.bundle import write_json_atomic
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe


def capture_baseline(baseline_id: str) -> dict[str, Any]:
    manifest = load_json_ref("config/uve/uve_regression_manifest.json")
    spec = manifest["baselines"][baseline_id]
    probes = [run_probe(spec["profile_ref"], pid) for pid in spec["probe_ids"]]
    from agent_training.uve.bundle import load_manifest_bundle

    bundle = load_manifest_bundle()
    gate = evaluate_gate(
        spec["gate_id"],
        probes,
        manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
        gate_evaluator_ref="regression_capture",
    )
    if gate["decision"] != "approved":
        raise ValueError("baseline capture requires an approved gate")
    payload = {
        "baseline_id": baseline_id,
        "gate_decision": gate["decision"],
        "gate_receipt": gate,
        "probe_run_ids": [p["probe_run_id"] for p in probes],
        "fingerprint_sha256": content_hash(
            {"gate": gate["decision"], "probes": [p["verdict"] for p in probes]}
        ),
    }
    root = pipeline_root()
    store = root / manifest["baseline_store_relative"]
    write_json_atomic(store / f"{baseline_id}.json", payload)
    return payload


def _fingerprint_for_spec(spec: dict[str, Any]) -> str:
    from agent_training.uve.bundle import load_manifest_bundle

    probes = [run_probe(spec["profile_ref"], pid) for pid in spec["probe_ids"]]
    bundle = load_manifest_bundle()
    gate = evaluate_gate(
        spec["gate_id"],
        probes,
        manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
        gate_evaluator_ref="regression_compare",
    )
    return content_hash({"gate": gate["decision"], "probes": [p["verdict"] for p in probes]})


def compare_baseline(baseline_id: str) -> dict[str, Any]:
    manifest = load_json_ref("config/uve/uve_regression_manifest.json")
    root = pipeline_root()
    path = root / manifest["baseline_store_relative"] / f"{baseline_id}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    spec = manifest["baselines"][baseline_id]
    fresh_fp = _fingerprint_for_spec(spec)
    regressed = stored["fingerprint_sha256"] != fresh_fp
    return {"baseline_id": baseline_id, "regressed": regressed, "stored_fingerprint": stored["fingerprint_sha256"], "fresh_fingerprint": fresh_fp}
