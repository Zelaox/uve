from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from agent_training.manifest_loader import load_json_ref, pipeline_root
from agent_training.uve.bundle import load_manifest_bundle, write_json_atomic, resolve_log_dir
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe


def run_witness_bundle(candidate_root: Path | None = None) -> dict:
    witness = load_json_ref("config/uve/uve_witness_manifest.json")
    profile_ref = witness["profile_ref"]
    candidate_root = (candidate_root or pipeline_root()).resolve()
    from agent_training.uve.leases import LeaseManager
    manager = LeaseManager()
    token = manager.acquire("witness_instance", str(candidate_root), witness["witness_evaluator_ref"])
    if token is None:
        return {"approved": False, "status": "not_verified", "reason": "witness lease denied"}
    run_dir = resolve_log_dir() / witness["run_store_relative"] / uuid.uuid4().hex
    candidate = run_dir / "candidate"
    try:
        for relative in witness["candidate_directories"]:
            shutil.copytree(candidate_root / relative, candidate / relative,
                            ignore=shutil.ignore_patterns(*witness["copy_ignore_patterns"]))
        config = run_dir / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        environment = dict(os.environ, PYTHONPATH=str(candidate))
        tests = [str(pipeline_root() / ref) for ref in witness["contract_test_refs"]]
        proc = subprocess.run([sys.executable, "-m", "pytest", "-c", str(config),
                               "--import-mode=importlib", "-p", "no:cacheprovider", "--basetemp", str(run_dir / "tmp"), "-q", *tests],
                              cwd=candidate, env=environment, capture_output=True, text=True,
                              timeout=witness["timeout_seconds"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        candidate_passed = proc.returncode == 0
        execution = {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:
        candidate_passed = False
        execution = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        manager.release("witness_instance", str(candidate_root), token)
    probes = []
    for pid in witness["golden_probe_ids"]:
        probes.append(run_probe(profile_ref, pid, dry_mode=None if candidate_passed else "fail_subject"))
    bundle = load_manifest_bundle()
    gate = evaluate_gate(
        witness["golden_gate_id"],
        probes,
        manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
        gate_evaluator_ref=witness["witness_evaluator_ref"],
    )
    if not candidate_passed:
        gate["decision"] = "rejected"
        gate["rejection_basis"] = "engine_fault"
    result = {
        "witness_evaluator_ref": witness["witness_evaluator_ref"],
        "gate": gate,
        "approved": gate["decision"] == "approved",
        "candidate_execution": execution,
        "candidate_root": str(candidate_root),
    }
    log_dir = resolve_log_dir()
    write_json_atomic(log_dir / "witness" / "last_run.json", result)
    write_json_atomic(log_dir / "gates" / "witness_gate.json", gate)
    if result["approved"]:
        write_json_atomic(log_dir / "witness" / "last_approved.json", result)
    return result
