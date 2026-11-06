from __future__ import annotations

import subprocess
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from agent_training.manifest_loader import load_json_ref

_ui_dump_locks: dict[str, threading.Lock] = {}


def _adb_run(spec: dict[str, Any], args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    if not spec.get("adb_executable") or not spec.get("adb_serial"):
        raise ValueError("ADB probe requires an explicitly selected executable and serial")
    cmd = [spec["adb_executable"], "-s", spec["adb_serial"], *args]
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _dump_lock(spec: dict[str, Any]) -> threading.Lock:
    serial = str(spec.get("adb_serial") or "default")
    return _ui_dump_locks.setdefault(serial, threading.Lock())


def _parse_hierarchy_xml(xml: str) -> ET.Element:
    start, end = xml.find("<hierarchy"), xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise ValueError("ADB did not return UI hierarchy")
    return ET.fromstring(xml[start:end + len("</hierarchy>")])


def _read_ui_hierarchy_once(spec: dict[str, Any], timeout: float) -> ET.Element:
    direct = _adb_run(spec, ["shell", "uiautomator", "dump", "/dev/tty"], timeout)
    xml = direct.stdout or ""
    if direct.returncode == 0 and "<hierarchy" in xml:
        return _parse_hierarchy_xml(xml)

    temporary = _adb_run(spec, ["shell", "mktemp"], timeout)
    remote = (temporary.stdout or "").strip()
    if temporary.returncode == 0 and re.fullmatch(r"/[A-Za-z0-9_./-]+", remote) \
            and not remote.endswith("/") and all(part not in (".", "..") for part in remote.split("/")[1:]):
        try:
            dump = _adb_run(spec, ["shell", "uiautomator", "dump", remote], timeout)
            if dump.returncode == 0:
                document = _adb_run(spec, ["shell", "cat", remote], timeout)
                xml = document.stdout or ""
                if document.returncode == 0 and "<hierarchy" in xml:
                    return _parse_hierarchy_xml(xml)
        finally:
            _adb_run(spec, ["shell", "rm", "--", remote], timeout)

    fallback_remote = "/sdcard/uve_window_dump.xml"
    dump = _adb_run(spec, ["shell", "uiautomator", "dump", fallback_remote], timeout)
    if dump.returncode != 0:
        raise ValueError("ADB could not dump UI hierarchy")
    document = _adb_run(spec, ["shell", "cat", fallback_remote], timeout)
    xml = document.stdout or ""
    if document.returncode != 0 or "<hierarchy" not in xml:
        raise ValueError("ADB did not return UI hierarchy")
    _adb_run(spec, ["shell", "rm", "--", fallback_remote], timeout)
    return _parse_hierarchy_xml(xml)


def _read_ui_hierarchy(spec: dict[str, Any], timeout: float, *, retry_count: int = 3) -> ET.Element:
    lock = _dump_lock(spec)
    last_error: Exception | None = None
    with lock:
        for attempt in range(retry_count):
            try:
                return _read_ui_hierarchy_once(spec, timeout)
            except ValueError as exc:
                last_error = exc
                if attempt + 1 < retry_count:
                    time.sleep(0.35 * (attempt + 1))
        raise ValueError(str(last_error) if last_error else "ADB could not dump UI hierarchy")


def _run_ui_flow(
    spec: dict[str, Any],
    flow_ref: str,
    fault_policy_ref: str | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flow = load_json_ref(flow_ref)
    timeout = float(spec["timeout_seconds"])
    step_results: list[dict[str, Any]] = []
    def guard(tree=None):
        if not fault_policy_ref:
            return None
        from agent_training.uve.adapters.ui_faults import ui_fault_result
        return ui_fault_result(tree if tree is not None else _read_ui_hierarchy(spec, timeout), fault_policy_ref)
    if failure := guard():
        return failure
    for step in flow["steps"]:
        kind = step["kind"]
        if kind == "shell_property":
            proc = _adb_run(spec, ["shell", "getprop", step["property"]], timeout)
            value = (proc.stdout or "").strip()
            ok = proc.returncode == 0 and value == step["expected"]
            step_results.append({"step_id": step["step_id"], "ok": ok, "observed": value})
            if not ok:
                return {
                    "verdict": "failed",
                    "fault_origin": "subject_fault",
                    "failure_class": "assertion_mismatch",
                    "ui_steps": step_results,
                    "failed_step": step["step_id"],
                }
        elif kind == "shell_dump_window":
            pkg_doc = load_json_ref(step["package_substring_ref"])
            needle = pkg_doc["package_substring"]
            if not needle:
                return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "no selected application package"}
            proc = _adb_run(spec, ["shell", "dumpsys", "window"], timeout)
            body = "\n".join(line for line in (proc.stdout or "").splitlines() if "mCurrentFocus" in line or "mFocusedApp" in line)
            ok = proc.returncode == 0 and needle in body
            step_results.append({"step_id": step["step_id"], "ok": ok, "needle": needle})
            if not ok:
                return {
                    "verdict": "failed",
                    "fault_origin": "subject_fault",
                    "failure_class": "assertion_mismatch",
                    "ui_steps": step_results,
                    "failed_step": step["step_id"],
                }
        elif kind == "navigation_recovery":
            if not profile:
                return {
                    "verdict": "not_verified",
                    "fault_origin": "engine_fault",
                    "failure_class": "binding_missing",
                    "reason": "navigation_recovery requires application profile bindings",
                }
            policy_ref = step.get("policy_ref")
            if not policy_ref:
                return {
                    "verdict": "not_verified",
                    "fault_origin": "engine_fault",
                    "failure_class": "binding_missing",
                    "reason": "navigation_recovery step missing policy_ref",
                }
            from agent_training.uve.adapters.ui_navigation_recovery import run_navigation_recovery_step

            def adb_run(args: list[str], adb_timeout: float):
                return _adb_run(spec, args, adb_timeout)

            recovery = run_navigation_recovery_step(
                spec=spec,
                policy_ref=policy_ref,
                profile=profile,
                read_hierarchy=lambda: _read_ui_hierarchy(spec, timeout),
                adb_run=adb_run,
                timeout=timeout,
            )
            ok = recovery.get("verdict") == "passed"
            step_results.append(
                {
                    "step_id": step["step_id"],
                    "ok": ok,
                    "recovery_trace": recovery.get("recovery_trace"),
                    "attempts": recovery.get("attempts"),
                }
            )
            if not ok:
                return {
                    **recovery,
                    "ui_steps": step_results,
                    "failed_step": step["step_id"],
                }
        elif kind in ("assert_element", "tap_element"):
            try:
                tree = _read_ui_hierarchy(spec, timeout)
            except ValueError as exc:
                return {
                    "verdict": "not_verified",
                    "fault_origin": "engine_fault",
                    "failure_class": "uiautomator_unavailable",
                    "error": str(exc),
                    "ui_steps": step_results,
                    "failed_step": step["step_id"],
                }
            if failure := guard(tree):
                return {**failure, "failed_step": step["step_id"], "ui_steps": step_results}
            selector = step["selector"]
            if not selector:
                raise ValueError("element assertion requires a selector")
            node = next((n for n in tree.iter("node") if all(n.get(k) == v for k, v in selector.items())), None)
            ok = node is not None
            if ok and kind == "tap_element":
                coords = [int(v) for v in re.findall(r"\d+", node.get("bounds", ""))]
                if len(coords) != 4:
                    raise ValueError("selected element has invalid bounds")
                tap = _adb_run(spec, ["shell", "input", "tap", str((coords[0]+coords[2])//2), str((coords[1]+coords[3])//2)], timeout)
                if tap.returncode != 0:
                    raise ValueError("ADB tap failed")
            step_results.append({"step_id": step["step_id"], "ok": ok, "selector": selector})
            if not ok:
                return {"verdict": "failed", "fault_origin": "subject_fault", "failure_class": "assertion_mismatch",
                        "ui_steps": step_results, "failed_step": step["step_id"]}
        elif kind in ("input_text", "swipe", "keyevent"):
            import shlex
            if kind == "input_text":
                args = ["text", shlex.quote(step["text"].replace(" ", "%s"))]
            elif kind == "swipe":
                args = ["swipe", *[str(int(v)) for v in step["coordinates"]], str(int(step["duration_ms"]))]
            else:
                args = ["keyevent", str(int(step["keycode"]))]
            proc = _adb_run(spec, ["shell", "input", *args], timeout)
            if proc.returncode != 0:
                raise ValueError(f"ADB {kind} failed")
            step_results.append({"step_id": step["step_id"], "ok": True})
        elif kind == "assert_activity":
            proc = _adb_run(spec, ["shell", "dumpsys", "activity", "activities"], timeout)
            resumed = "\n".join(line for line in (proc.stdout or "").splitlines() if "ResumedActivity" in line or "mResumedActivity" in line)
            ok = proc.returncode == 0 and step["expected_substring"] in resumed
            step_results.append({"step_id": step["step_id"], "ok": ok})
            if not ok:
                return {"verdict": "failed", "fault_origin": "subject_fault", "failure_class": "assertion_mismatch",
                        "ui_steps": step_results, "failed_step": step["step_id"]}
        else:
            return {
                "verdict": "not_verified",
                "fault_origin": "engine_fault",
                "reason": f"unknown ui step kind {kind}",
            }
    if not step_results or not any(s["kind"].startswith("assert_") or "expected" in s or "assertion" in s for s in flow["steps"]):
        return {"verdict": "not_verified", "fault_origin": "engine_fault", "reason": "flow requires assertions"}
    if failure := guard():
        return {**failure, "ui_steps": step_results}
    return {"verdict": "passed", "ui_steps": step_results, "outcome_token": "ui_flow_ok"}


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    profile = ctx["profile"]
    probe_spec = ctx.get("probe_spec", {})
    adb_ref = profile.get("adb_probe_ref")
    if not adb_ref:
        return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "no adb_probe_ref"}
    spec = load_json_ref(adb_ref)
    missing = [field for field in ("adb_executable", "adb_serial") if not spec.get(field)]
    if missing:
        return {"verdict": "not_verified", "fault_origin": "unknown",
                "reason": "missing runtime bindings", "missing_fields": missing}

    flow_ref = profile.get("ui_flow_ref") or probe_spec.get("ui_flow_ref")
    if probe_id == "application_membership_ui_flow":
        flow_ref = profile.get("membership_ui_flow_ref") or flow_ref
    if probe_id in ("adb_ui_flow", "application_membership_ui_flow") and flow_ref:
        if ctx.get("dry_mode") == "ui_fail":
            return {
                "verdict": "failed",
                "fault_origin": "subject_fault",
                "failure_class": "assertion_mismatch",
                "ui_steps": [{"step_id": "dry", "ok": False}],
            }
        if ctx.get("dry_mode") == "skip_adb":
            return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "dry skip"}
        result = _run_ui_flow(spec, flow_ref, probe_spec.get("fault_policy_ref"), profile=profile)
        screenshot = load_json_ref(flow_ref).get("screenshot")
        if screenshot:
            from agent_training.uve.evidence_store import EvidenceStore
            proc = subprocess.run([spec["adb_executable"], "-s", spec["adb_serial"], "exec-out", "screencap", "-p"],
                                  capture_output=True, timeout=spec["timeout_seconds"])
            if proc.returncode != 0 or not proc.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("ADB screenshot capture failed")
            artifact = EvidenceStore().write_artifact(ctx["role_id"], proc.stdout, media_type="image/png")
            result["artifacts"] = [artifact]
        return result

    prop = spec["boot_property"]
    try:
        proc = _adb_run(spec, ["shell", "getprop", prop], float(spec["timeout_seconds"]))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "verdict": "not_verified",
            "fault_origin": "engine_fault",
            "failure_class": "adb_boot_incomplete",
            "error": str(exc),
        }
    value = (proc.stdout or "").strip()
    expected = spec["expected_value"]
    if proc.returncode != 0:
        return {"verdict": "not_verified", "fault_origin": "engine_fault", "error": proc.stderr}
    if value == expected:
        return {"outcome_token": f"{prop}={value}", "verdict": "passed"}
    return {
        "verdict": "failed",
        "fault_origin": "subject_fault",
        "failure_class": "adb_boot_incomplete",
        "observed": value,
        "expected": expected,
    }
