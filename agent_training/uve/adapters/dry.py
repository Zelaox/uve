from __future__ import annotations

from typing import Any


def run_probe(*, probe_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    expectation = ctx["probe_spec"]
    _ = expectation
    mode = ctx.get("dry_mode", "pass")
    if mode == "fail_subject":
        return {
            "outcome_token": "echo_fail",
            "verdict": "failed",
            "fault_origin": "subject_fault",
            "failure_class": "assertion_mismatch",
        }
    return {"outcome_token": "echo_ok", "verdict": "passed", "fault_origin": "none"}
