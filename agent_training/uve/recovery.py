from __future__ import annotations

import subprocess
import os
import time
from typing import Any

from agent_training.manifest_loader import load_json_ref
from agent_training.uve.bundle import resolve_log_dir, write_json_atomic
from agent_training.uve.lease_policy import acquire_probe_lease, release_probe_lease
from agent_training.uve.orchestrator import run_probe
from agent_training.uve.workspace import resolve_workspace_root

_owned: dict[str, subprocess.Popen] = {}


def run_recovery_drill(profile_ref: str, action_id: str) -> dict[str, Any]:
    manifest = load_json_ref("config/uve/uve_recovery_manifest.json")
    profile = load_json_ref(profile_ref)
    policy = manifest.get("profiles", {}).get(profile["project_id"], {})
    if action_id not in policy.get("allowed_actions", []) or not profile.get("recovery", {}).get("automatic_restart"):
        return {"status": "denied", "reason": "action not allowed for profile"}
    action = manifest["actions"][action_id]
    spec_ref = profile.get("recovery_action_refs", {}).get(action_id)
    if not spec_ref:
        return {"status": "not_verified", "reason": "no recovery command manifest"}
    spec = load_json_ref(spec_ref)
    lease, denied = acquire_probe_lease(profile, action_id, holder="recovery")
    if denied:
        return {"status": "not_verified", "reason": "lease denied", "lease": denied}
    result: dict[str, Any] = {"status": "not_verified", "action_id": action_id,
                              "recovery_chain": [], "reverification": []}
    try:
        root = resolve_workspace_root(profile)
        owner = f"{root}:{action_id}"
        process = _owned.get(owner)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=spec["stop_timeout_seconds"])
            result["recovery_chain"].append({"step": "stop_owned_process", "pid": process.pid})
        output = resolve_log_dir() / "recovery" / spec["log_filename"]
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("ab") as stream:
            process = subprocess.Popen(spec["start_argv"], cwd=root, stdout=stream, stderr=stream,
                                       env=dict(os.environ, **spec.get("environment", {})),
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _owned[owner] = process
        result["recovery_chain"].append({"step": "start_process", "pid": process.pid, "log": str(output)})
        probe_ids = action["requires_reverification_probe_ids"]
        if not probe_ids:
            result["reason"] = "recovery requires reverification probes"
            return result
        for attempt in range(spec["retest_attempts"]):
            receipts = [run_probe(profile_ref, pid, run_lease_id=lease) for pid in probe_ids]
            result["reverification"].extend(receipts)
            if all(r["verdict"] == "passed" for r in receipts):
                result["status"] = "complete"
                break
            if process.poll() is not None:
                result["reason"] = f"started process exited: {process.returncode}"
                break
            if attempt + 1 < spec["retest_attempts"]:
                time.sleep(spec["retest_interval_seconds"])
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        release_probe_lease(profile, action_id, lease)
        write_json_atomic(resolve_log_dir() / "recovery" / "last_run.json", result)
    return result
