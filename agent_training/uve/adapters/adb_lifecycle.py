from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any

from agent_training.manifest_loader import load_json_ref
from agent_training.uve.adapters.adb import _adb_run
from agent_training.uve.bundle import resolve_log_dir
from agent_training.uve.evidence_store import EvidenceStore


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _run_lifecycle(ctx)
    except Exception as exc:
        result = {"verdict": "not_verified", "fault_origin": "engine_fault", "failure_class": "adapter_error",
                  "error": f"{type(exc).__name__}: {exc}"}
    log = resolve_log_dir() / "lifecycle" / f"{ctx['probe_run_id']}.log"
    if log.is_file():
        result.setdefault("artifacts", []).append(EvidenceStore().write_artifact(
            ctx["role_id"], log.read_bytes(), media_type="text/plain"))
    return result


def _run_lifecycle(ctx: dict[str, Any]) -> dict[str, Any]:
    reference = ctx["profile"].get("adb_lifecycle_ref")
    if not reference:
        return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "no lifecycle manifest"}
    spec = load_json_ref(reference)
    timeout = spec["command_timeout_seconds"]
    inventory = subprocess.run([spec["adb_executable"], "devices"], capture_output=True, text=True, timeout=timeout)
    if inventory.returncode:
        raise RuntimeError("ADB transport inventory unavailable")
    for line in inventory.stdout.splitlines()[1:]:
        fields = line.split()
        if not fields:
            continue
        if fields[0] == spec["adb_serial"]:
            return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "lifecycle serial already in use"}
        guest = dict(spec, adb_serial=fields[0])
        name = _adb_run(guest, ["emu", "avd", "name"], timeout)
        if spec["avd_id"] in name.stdout.splitlines():
            return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "AVD already running on another serial"}
    executable = os.environ[spec["emulator_executable_env"]]
    log = resolve_log_dir() / "lifecycle" / f"{ctx['probe_run_id']}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    steps = [{"state": "starting"}]
    process = None
    try:
        with log.open("wb") as output:
            process = subprocess.Popen([executable, *spec["launch_argv"]], stdout=output, stderr=output,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + spec["boot_timeout_seconds"]
        last_state = None
        while time.monotonic() < deadline:
            state = _adb_run(spec, ["get-state"], timeout)
            observed = state.stdout.strip() if state.returncode == 0 else "offline"
            if observed != last_state:
                steps.append({"state": observed})
                last_state = observed
            boot = _adb_run(spec, ["shell", "getprop", spec["boot_property"]], timeout)
            if boot.returncode == 0 and boot.stdout.strip() == spec["expected_boot_value"]:
                steps.append({"state": "running", "boot_value": boot.stdout.strip()})
                break
            if process.poll() is not None:
                raise RuntimeError(f"emulator exited: {process.returncode}; see {log}")
            time.sleep(spec["poll_interval_seconds"])
        else:
            return {"verdict": "failed", "fault_origin": "subject_fault", "failure_class": "adb_boot_incomplete", "lifecycle_steps": steps}
        screenshot = subprocess.run([spec["adb_executable"], "-s", spec["adb_serial"], "exec-out", "screencap", "-p"],
                                    capture_output=True, timeout=timeout)
        if screenshot.returncode or not screenshot.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("guest display capture unavailable")
        artifact = EvidenceStore().write_artifact(ctx["role_id"], screenshot.stdout, media_type="image/png")
        steps.append({"state": "display_reachable", "content_sha256": artifact["content_sha256"]})
        stop = _adb_run(spec, ["emu", "kill"], spec["stop_timeout_seconds"])
        if stop.returncode:
            raise RuntimeError("graceful emulator stop failed")
        process.wait(timeout=spec["stop_timeout_seconds"])
        state = _adb_run(spec, ["get-state"], timeout)
        if state.returncode == 0:
            raise RuntimeError("guest transport remains after stop")
        steps.append({"state": "stopped", "transport_disappeared": True, "process_exit_code": process.returncode})
        return {"verdict": "passed", "lifecycle_steps": steps, "artifacts": [artifact]}
    finally:
        if process and process.poll() is None:
            try:
                _adb_run(spec, ["emu", "kill"], spec["stop_timeout_seconds"])
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                process.wait(timeout=spec["stop_timeout_seconds"])
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=spec["stop_timeout_seconds"])
                else:
                    process.terminate()
                process.wait(timeout=spec["stop_timeout_seconds"])
