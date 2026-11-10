"""Compile a deep-flow contract before any probe step can touch its target."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import ManifestError, load_json_ref


class FlowContractError(ManifestError):
    pass


TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
MISSING = object()


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise FlowContractError(reason)


def token(value: Any, field: str) -> str:
    require(isinstance(value, str) and TOKEN.fullmatch(value), f"invalid {field}")
    return value


def at(value: Any, path: list[str]) -> Any:
    for name in path:
        if not isinstance(value, Mapping) or name not in value:
            return MISSING
        value = value[name]
    return value


def typed(value: Any, spec: dict[str, Any]) -> Any:
    """Only types safe for a projected receipt. Arbitrary text stays out."""
    kind = spec["type"]
    if kind == "integer" and type(value) is int:
        return value
    if kind == "boolean" and type(value) is bool:
        return value
    if kind == "digest" and isinstance(value, str) and DIGEST.fullmatch(value):
        return value
    if kind == "enum" and isinstance(value, str) and value in spec["values"]:
        return value
    if kind == "integer_list" and isinstance(value, list) and all(type(item) is int for item in value):
        return value
    return MISSING


def field_spec(spec: dict[str, Any]) -> None:
    require(isinstance(spec, dict), "field schema must be an object")
    require(spec.get("type") in {"integer", "boolean", "digest", "enum", "integer_list"}, "unsupported field type")
    if spec["type"] == "enum":
        require(isinstance(spec.get("values"), list) and spec["values"], "enum needs an allowlist")
        for value in spec["values"]:
            token(value, "enum value")


def field_path(value: Any) -> list[str]:
    require(isinstance(value, list) and value and all(isinstance(part, str) and part for part in value), "invalid field path")
    return value


def _unique(items: Any, key: str, label: str) -> dict[str, dict]:
    require(isinstance(items, list) and items, f"missing {label}")
    result = {}
    for item in items:
        require(isinstance(item, dict), f"invalid {label}")
        item_id = token(item.get(key), key)
        require(item_id not in result, f"duplicate {key}: {item_id}")
        result[item_id] = item
    return result


def _ordered_steps(case: dict, adapter_policy: dict) -> list[dict]:
    steps = _unique(case.get("steps"), "step_id", "steps")
    ordered: list[dict] = []
    done: set[str] = set()
    cleanup_seen = False
    for step_id, step in steps.items():
        adapter = step.get("adapter")
        require(adapter in adapter_policy, f"unavailable step adapter: {step_id}")
        phase = step.get("phase")
        require(phase in {"setup", "action", "observe", "fault", "cleanup"}, f"invalid phase: {step_id}")
        require(not cleanup_seen or phase == "cleanup", "cleanup steps must be last")
        cleanup_seen |= phase == "cleanup"
        after = step.get("after", [])
        require(isinstance(after, list) and all(parent in done for parent in after), f"unbound or cyclic dependency: {step_id}")
        require(len(set(after)) == len(after), f"duplicate dependency: {step_id}")
        if phase == "fault":
            require(adapter_policy[adapter].get("fault_capable") is True, "fault adapter is not allowlisted")
        else:
            require(adapter_policy[adapter].get("fault_capable") is not True, "fault adapter requires the fault phase")
        for name, spec in step.get("fields", {}).items():
            token(name, "observation field")
            field_spec(spec)
            field_path(spec.get("path"))
        if "trace" in step:
            trace = step["trace"]
            require(isinstance(trace, dict), "trace schema must be an object")
            token(trace.get("source"), "trace source")
            kinds = trace.get("kinds", [])
            require(isinstance(kinds, list) and kinds, "trace kinds must be declared")
            for kind in kinds:
                token(kind, "trace kind")
            if "observe_kind" in trace:
                require(trace["observe_kind"] in kinds, "generated observation kind is not declared")
                require(all(parent in after for parent in trace.get("parents_from", [])), "trace parents must be step dependencies")
        ordered.append(step)
        done.add(step_id)
    require(any(step["phase"] == "setup" for step in ordered), "fixture start must be observed")
    require(any(step["phase"] == "observe" for step in ordered), "result must be observed")
    return ordered


ARITY = {"eq": 2, "ne": 2, "lte": 2, "all_eq": None, "set_eq": 2, "true": 1}


def _validate_checks(case: dict, bindings: dict, policy: dict, depth: int) -> None:
    steps = {step["step_id"]: step for step in case["steps"]}
    observations = {key: set(step.get("fields", {})) for key, step in steps.items()}
    checks = _unique(case.get("invariants"), "invariant_id", "invariants")
    for key, check in checks.items():
        op = check.get("operator")
        require(op in ARITY, f"unsupported invariant operator: {key}")
        require(check.get("severity") in policy["severity"], f"unknown invariant severity: {key}")
        refs = check.get("refs", [])
        require(isinstance(refs, list) and len(refs) >= 1, f"missing invariant operands: {key}")
        require(len(refs) == ARITY[op] if ARITY[op] else len(refs) >= 2, f"invalid invariant arity: {key}")
        for ref in refs:
            require(isinstance(ref, dict), f"invalid operand: {key}")
            if set(ref) == {"binding"}:
                require(ref["binding"] in bindings, f"unbound invariant operand: {key}")
            else:
                require(set(ref) == {"step", "field"} and ref["field"] in observations.get(ref["step"], ()),
                        f"unbound invariant observation: {key}")
    require(any(any(ref.get("step") in steps and steps[ref["step"]]["phase"] == "observe"
                    for ref in check["refs"]) for check in checks.values()), "invariants must check an observed result")
    if depth >= 5:
        edges = _unique(case.get("trace_edges"), "edge_id", "trace edges")
        for edge in edges.values():
            for end in ("from", "to"):
                endpoint = edge.get(end, {})
                step = steps.get(endpoint.get("step"), {})
                require(endpoint.get("kind") in step.get("trace", {}).get("kinds", ()), "unbound trace edge endpoint")
            require(edge.get("relation", "causal") in {"causal", "new_attempt", "new_epoch"}, "invalid trace relation")
        require(any(edge.get("different_sources") is True for edge in edges.values()), "trace needs independent sources")
        authorities = policy["adapters"]
        for edge in edges.values():
            if edge.get("different_sources") is True:
                left = steps[edge["from"]["step"]]
                right = steps[edge["to"]["step"]]
                require(authorities[left["adapter"]]["authority"] != authorities[right["adapter"]]["authority"],
                        "independent trace sources require different adapter authorities")
    if case["kind"] == "fault":
        faults = [step for step in steps.values() if step["phase"] == "fault"]
        require(len(faults) == 1 and faults[0].get("after"), "fault scenario needs one observed injection boundary")
        require(any(step["phase"] == "cleanup" for step in steps.values()), "fault scenario needs cleanup")


def compile_flow(ref: str, policy: dict, depth: int) -> dict[str, Any]:
    source = load_json_ref(ref)
    require(source.get("schema_version") == "uve_deep_flow.v1", "unsupported deep flow schema")
    flow_id = token(source.get("flow_id"), "flow id")
    bindings, origins = {}, {}
    for key, spec in source.get("bindings", {}).items():
        token(key, "binding name")
        field_spec(spec)
        path = field_path(spec.get("path"))
        doc = load_json_ref(spec["value_ref"])
        value = typed(at(doc, path), spec)
        require(value is not MISSING, f"missing or invalid binding: {key}")
        bindings[key] = value
        origins[key] = {"manifest_sha256": doc["_content_hash"], "path": path}
    cases = _unique(source.get("cases"), "case_id", "cases")
    for case in cases.values():
        require(case.get("kind") in {"baseline", "fault"}, "unknown case kind")
    selected = sorted((case for case in cases.values() if case["kind"] == "baseline" or depth >= 6),
                      key=lambda case: case["kind"] == "fault")
    require(any(case["kind"] == "baseline" for case in selected), "a baseline case is required")
    if depth >= 6:
        require(any(case["kind"] == "fault" for case in selected), "V6 needs a fault scenario")
    compiled_cases = []
    for case in selected:
        compiled = {**case, "steps": _ordered_steps(case, policy["adapters"])}
        require(compiled["kind"] == "fault" or all(step["phase"] != "fault" for step in compiled["steps"]),
                "baseline cannot inject faults")
        _validate_checks(compiled, bindings, policy, depth)
        compiled_cases.append(compiled)
    compiled = {"flow_id": flow_id, "depth": depth, "cases": compiled_cases,
                "bindings": bindings, "binding_origins": origins, "flow_sha256": source["_content_hash"],
                "policy_sha256": policy["_content_hash"]}
    return {**compiled, "plan_sha256": content_hash(compiled)}
