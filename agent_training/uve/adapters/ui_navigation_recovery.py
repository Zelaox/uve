from __future__ import annotations

import re
import time
from typing import Any, Callable
from xml.etree import ElementTree as ET

from agent_training.manifest_loader import load_json_ref


ReadHierarchy = Callable[[], ET.Element]
AdbRun = Callable[[list[str], float], Any]


def _node_matches(node: ET.Element, selector: dict[str, str]) -> bool:
    return all(node.get(key) == value for key, value in selector.items())


def any_selector_matches(tree: ET.Element, selectors: list[dict[str, str]]) -> bool:
    for selector in selectors:
        for node in tree.iter("node"):
            if _node_matches(node, selector):
                return True
    return False


def find_node_by_selector(tree: ET.Element, selector: dict[str, str]) -> ET.Element | None:
    for node in tree.iter("node"):
        if _node_matches(node, selector):
            return node
    return None


def text_markers_present(tree: ET.Element, markers: list[str], *, contains: bool = False) -> bool:
    for node in tree.iter("node"):
        text = node.get("text") or ""
        for marker in markers:
            if contains:
                if marker.lower() in text.lower():
                    return True
            elif text == marker:
                return True
    return False


def focused_package_includes(needle: str, timeout: float, adb_run: AdbRun) -> bool:
    proc = adb_run(["shell", "dumpsys", "window"], timeout)
    body = proc.stdout or ""
    if proc.returncode != 0:
        return False
    focus = "\n".join(
        line for line in body.splitlines() if "mCurrentFocus" in line or "mFocusedApp" in line
    )
    return needle in focus


def resolve_guest_needle(policy: dict[str, Any], profile: dict[str, Any]) -> str:
    profile_field = policy.get("guest_package_profile_field")
    if profile_field:
        profile_ref = profile.get(profile_field)
        if profile_ref:
            doc = load_json_ref(profile_ref)
            needle = doc.get("package_substring")
            if needle:
                return str(needle)
    guest_ref = policy.get("guest_package_ref")
    if not guest_ref:
        raise ValueError("navigation recovery policy missing guest package binding")
    doc = load_json_ref(guest_ref)
    needle = doc.get("package_substring")
    if not needle:
        raise ValueError("guest package manifest missing package_substring")
    return str(needle)


def resolve_experience_uri(policy: dict[str, Any], profile: dict[str, Any]) -> str:
    field = policy.get("runtime_context_profile_field", "runtime_context_ref")
    context_ref = profile.get(field)
    if not context_ref:
        raise ValueError("navigation recovery requires runtime context on profile")
    context = load_json_ref(context_ref)
    port = str(context.get("dev_server_port") or "").strip()
    if not port.isdigit():
        raise ValueError("runtime context dev_server_port missing for navigation recovery")
    template = policy.get("experience_uri_template")
    if not template or "{dev_server_port}" not in template:
        raise ValueError("navigation recovery policy missing experience_uri_template")
    return template.replace("{dev_server_port}", port)


def run_navigation_recovery_step(
    *,
    spec: dict[str, Any],
    policy_ref: str,
    profile: dict[str, Any],
    read_hierarchy: ReadHierarchy,
    adb_run,
    timeout: float,
) -> dict[str, Any]:
    policy = load_json_ref(policy_ref)
    try:
        guest_needle = resolve_guest_needle(policy, profile)
    except ValueError as exc:
        return {
            "verdict": "not_verified",
            "fault_origin": "engine_fault",
            "failure_class": "binding_missing",
            "error": str(exc),
        }

    ready_selectors = policy.get("ready_when_any") or []
    settings_markers = policy.get("settings_markers_text") or []
    off_tabs_markers = policy.get("off_tabs_markers_text") or []
    overlay_markers = policy.get("blocking_overlay_text_contains") or []
    home_tab_selector = policy.get("home_tab_tap_selector")
    max_attempts = int(policy.get("max_attempts", 10))
    settle_ms = int(policy.get("settle_ms", 400))
    max_back_per_attempt = int(policy.get("max_back_keyevents_per_attempt", 3))
    recovery_trace: list[dict[str, Any]] = []

    try:
        experience_uri = resolve_experience_uri(policy, profile)
    except ValueError as exc:
        return {
            "verdict": "not_verified",
            "fault_origin": "engine_fault",
            "failure_class": "binding_missing",
            "error": str(exc),
        }

    for attempt in range(1, max_attempts + 1):
        try:
            tree = read_hierarchy()
        except ValueError as exc:
            return {
                "verdict": "not_verified",
                "fault_origin": "engine_fault",
                "failure_class": "uiautomator_unavailable",
                "error": str(exc),
                "recovery_trace": recovery_trace,
                "attempt": attempt,
            }

        if ready_selectors and any_selector_matches(tree, ready_selectors):
            return {
                "verdict": "passed",
                "recovery_trace": recovery_trace,
                "attempts": attempt,
                "outcome_token": "navigation_recovery_ready",
            }

        action = "wait"
        if not focused_package_includes(guest_needle, timeout, adb_run):
            action = "launch_experience"
            if policy.get("adb_reverse_before_launch"):
                port_match = re.search(r":(\d+)", experience_uri)
                if port_match:
                    port = port_match.group(1)
                    adb_run(["reverse", f"tcp:{port}", f"tcp:{port}"], timeout)
            launch = adb_run(
                ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", experience_uri],
                timeout,
            )
            if launch.returncode != 0:
                return {
                    "verdict": "not_verified",
                    "fault_origin": "engine_fault",
                    "failure_class": "adapter_error",
                    "error": "failed to launch experience URI",
                    "recovery_trace": recovery_trace,
                }
        elif overlay_markers and text_markers_present(tree, overlay_markers, contains=True):
            action = "dismiss_overlay_back"
            back = adb_run(["shell", "input", "keyevent", "4"], timeout)
            if getattr(back, "returncode", 1) != 0:
                return {
                    "verdict": "not_verified",
                    "fault_origin": "engine_fault",
                    "failure_class": "adapter_error",
                    "error": "BACK keyevent failed during overlay dismissal",
                    "recovery_trace": recovery_trace,
                }
        elif (settings_markers and text_markers_present(tree, settings_markers)) or (
            off_tabs_markers and text_markers_present(tree, off_tabs_markers)
        ):
            backs_used = 0
            action = "leave_settings_back"
            while backs_used < max_back_per_attempt:
                if ready_selectors and any_selector_matches(tree, ready_selectors):
                    break
                back = adb_run(["shell", "input", "keyevent", "4"], timeout)
                if getattr(back, "returncode", 1) != 0:
                    return {
                        "verdict": "not_verified",
                        "fault_origin": "engine_fault",
                        "failure_class": "adapter_error",
                        "error": "BACK keyevent failed leaving settings",
                        "recovery_trace": recovery_trace,
                    }
                backs_used += 1
                time.sleep(settle_ms / 1000.0)
                tree = read_hierarchy()
            recovery_trace.append({"attempt": attempt, "action": action, "back_keyevents": backs_used})
            if ready_selectors and any_selector_matches(tree, ready_selectors):
                return {
                    "verdict": "passed",
                    "recovery_trace": recovery_trace,
                    "attempts": attempt,
                    "outcome_token": "navigation_recovery_ready",
                }
            time.sleep(settle_ms / 1000.0)
            continue
        elif home_tab_selector:
            node = find_node_by_selector(tree, home_tab_selector)
            if node is not None:
                action = "tap_home_tab"
                coords = [int(v) for v in re.findall(r"\d+", node.get("bounds", ""))]
                if len(coords) != 4:
                    return {
                        "verdict": "not_verified",
                        "fault_origin": "engine_fault",
                        "failure_class": "adapter_error",
                        "error": "home tab selector had invalid bounds",
                        "recovery_trace": recovery_trace,
                    }
                tap = adb_run(
                    [
                        "shell",
                        "input",
                        "tap",
                        str((coords[0] + coords[2]) // 2),
                        str((coords[1] + coords[3]) // 2),
                    ],
                    timeout,
                )
                if getattr(tap, "returncode", 1) != 0:
                    return {
                        "verdict": "not_verified",
                        "fault_origin": "engine_fault",
                        "failure_class": "adapter_error",
                        "error": "tap home tab failed",
                        "recovery_trace": recovery_trace,
                    }
            else:
                action = "settle"
        else:
            action = "settle"

        recovery_trace.append({"attempt": attempt, "action": action})
        time.sleep(settle_ms / 1000.0)

    return {
        "verdict": "not_verified",
        "fault_origin": "engine_fault",
        "failure_class": "navigation_recovery_exhausted",
        "recovery_trace": recovery_trace,
        "max_attempts": max_attempts,
    }
