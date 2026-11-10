"""Evaluate and triage projected flow evidence without invoking an adapter."""

from __future__ import annotations

from typing import Any

from agent_training.uve.flow_contract import FlowContractError, MISSING
from agent_training.uve.flow_trace import check_edges


def _matches(operator: str, values: list[Any]) -> bool:
    if operator in {"eq", "ne", "all_eq"}:
        equal = all(type(value) is type(values[0]) and value == values[0] for value in values[1:])
        return not equal if operator == "ne" else equal
    if operator == "true":
        return values[0] is True
    if operator == "lte":
        return all(type(value) is int for value in values) and values[0] <= values[1]
    if operator == "set_eq":
        return all(isinstance(value, list) for value in values) and set(values[0]) == set(values[1])
    raise FlowContractError("unknown invariant operator")


def _invariants(case: dict, observations: dict, bindings: dict) -> list[dict]:
    result = []
    for check in case["invariants"]:
        values = [bindings[ref["binding"]] if "binding" in ref else
                  observations.get(ref["step"], {}).get("fields", {}).get(ref["field"], MISSING)
                  for ref in check["refs"]]
        missing = [ref for ref, value in zip(check["refs"], values) if value is MISSING]
        status = "not_verified" if missing else "passed" if _matches(check["operator"], values) else "failed"
        result.append({"invariant_id": check["invariant_id"], "severity": check["severity"], "state": status,
                       "operands": [None if value is MISSING else value for value in values], "missing": missing})
    return result


def evaluate(plan: dict, evidence: dict, policy: dict) -> dict:
    findings = []
    cases = []

    def finding(case_id: str, category: str, subject: str, state: str, details: dict) -> None:
        rule = policy["triage"][category]
        findings.append({"case_id": case_id, "category": category, "subject": subject, "state": state,
                         "certainty": "belagt" if state == "failed" else "saknar_bevis",
                         "rule_id": rule["rule_id"], "next_probe": rule["next_probe"], "details": details})

    for case in plan["cases"]:
        case_id = case["case_id"]
        snapshot = evidence.get(case_id, {})
        steps = snapshot.get("steps", {})
        for spec in case["steps"]:
            step_id = spec["step_id"]
            row = steps.get(step_id, {})
            state = row.get("state", "not_verified")
            if state != "passed":
                category = {"setup": "fixture", "fault": "injection", "cleanup": "cleanup"}.get(spec["phase"], "step")
                finding(case_id, category, step_id, state, {"reason": row.get("reason", "step did not run")})
        checks = _invariants(case, steps, plan["bindings"])
        for check in checks:
            if check["state"] != "passed":
                finding(case_id, "invariant", check["invariant_id"], check["state"], check)
        edges = []
        if plan["depth"] >= 5:
            try:
                edges = check_edges(snapshot.get("events", []), case["trace_edges"])
            except FlowContractError as exc:
                finding(case_id, "trace", case_id, "not_verified", {"reason": str(exc)})
            for edge in edges:
                if edge["state"] != "passed":
                    finding(case_id, "trace", edge["edge_id"], edge["state"], edge)
        if not snapshot.get("cleanup_verified"):
            finding(case_id, "cleanup", case_id, "not_verified", {"reason": "isolated fixture cleanup was not observed"})
        cases.append({"case_id": case_id, "kind": case["kind"], "invariants": checks, "edges": edges,
                      "steps": {name: row["state"] for name, row in steps.items()}})
    # A proven mismatch remains visible even if a secondary evidence gap exists.
    # The gate only attributes a failure to the product on a valid, complete run.
    has_gap = any(item["state"] == "not_verified" for item in findings)
    verdict = "not_verified" if has_gap else "failed" if findings else "passed"
    primary = min(findings, key=lambda item: policy["triage"][item["category"]]["priority"], default=None)
    return {"verdict": verdict, "cases": cases, "triage": {"primary": primary, "findings": findings}}
