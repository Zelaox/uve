"""Read-only explanations of runtime evidence; this module never evaluates a gate."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.uve.bundle import resolve_log_dir


def _receipt_path(root: Path, category: str, artifact_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", artifact_id):
        raise ValueError("receipt id must be a single artifact name")
    return root / category / f"{artifact_id}.json"


def diagnose_runtime(status_path: Path, context_path: Path | None = None) -> dict[str, Any]:
    root = resolve_log_dir()
    errors: list[str] = []
    report: dict[str, Any] = {"status_path": str(status_path.resolve()), "log_root": str(root),
                              "tiers": [], "evidence_errors": errors, "context_matches_status": None}
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        report["state"] = status["state"]
        report["worker_pid"] = status.get("worker_pid")
        if status.get("error"):
            report["runtime_error"] = status["error"]
        if context_path is not None:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            report["context_matches_status"] = content_hash(context) == status.get("context_sha256")
            report["context_path"] = str(context_path.resolve())
            report["selected_target"] = {key: context.get(key) for key in (
                "device_id", "device_state", "device_detail", "adb_serial", "sdk_root",
                "project_id", "project_root", "guest_package", "dev_server_port")}
        for tier in status["tiers"]:
            gate_snapshot = tier["gate"]
            gate_path = _receipt_path(root, "gates", gate_snapshot["trust_chain_ref"])
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            if gate != gate_snapshot:
                errors.append(f"gate differs from status: {gate_path}")
            result = {"tier_id": tier["tier_id"], "decision": gate["decision"],
                      "rejection_basis": gate.get("rejection_basis"),
                      "gate_receipt_path": str(gate_path), "probes": []}
            report["tiers"].append(result)
            for ref in gate["probe_receipt_refs"]:
                path = _receipt_path(root, "probes", ref["probe_run_id"])
                probe = json.loads(path.read_text(encoding="utf-8"))
                digest = content_hash({k: v for k, v in probe.items() if not k.startswith("_")})
                if digest != ref["receipt_sha256"] or probe["probe_run_id"] != ref["probe_run_id"]:
                    errors.append(f"probe hash or id mismatch: {path}")
                if probe["subject_identity"]["identity_sha256"] != gate["subject_identity_sha256"]:
                    errors.append(f"probe subject mismatch: {path}")
                result["probes"].append({"probe_id": probe["probe"]["probe_id"],
                    "probe_run_id": probe["probe_run_id"], "verdict": probe["verdict"],
                    "fault_origin": probe["fault_origin"], "failure_class": probe.get("failure_class"),
                    "observation": probe["observation"], "receipt_path": str(path)})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    report["evidence_consistent"] = not errors
    # Consistent historical evidence is not proof of current source or guest health.
    report["scope"] = "stored runtime evidence; application acceptance requires its own profile and gate"
    return report
