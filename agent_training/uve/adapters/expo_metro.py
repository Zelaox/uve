from __future__ import annotations

import urllib.error
import urllib.request
import errno
from typing import Any

from agent_training.manifest_loader import load_json_ref


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    profile = ctx["profile"]
    metro_ref = profile.get("metro_status_ref")
    if not metro_ref:
        return {"verdict": "not_verified", "fault_origin": "unknown", "reason": "no metro_status_ref"}
    metro = load_json_ref(metro_ref)
    url = metro["status_url"]
    expected = metro["required_body_substring"]
    try:
        with urllib.request.urlopen(url, timeout=metro["timeout_seconds"]) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        cause = getattr(exc, "reason", exc)
        if isinstance(cause, PermissionError) or getattr(cause, "errno", None) in (errno.EACCES, errno.EPERM):
            return {"verdict": "not_verified", "fault_origin": "engine_fault", "failure_class": "adapter_error", "error": str(exc)}
        return {
            "verdict": "failed",
            "fault_origin": "subject_fault",
            "failure_class": "metro_not_ready",
            "error": str(exc),
        }
    ok = expected in body
    from agent_training.content_hash import content_hash_file_text

    binding = content_hash_file_text(body)
    if ok:
        return {
            "outcome_token": expected,
            "verdict": "passed",
            "runtime_delivery_binding_sha256": binding,
        }
    return {
        "verdict": "failed",
        "fault_origin": "subject_fault",
        "failure_class": "metro_not_ready",
        "body_excerpt": body[:200],
    }
