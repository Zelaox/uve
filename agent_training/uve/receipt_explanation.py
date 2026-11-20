"""Build a bounded, evidence-linked explanation before UVE persists a receipt."""
from __future__ import annotations

from typing import Any

from agent_training.manifest_loader import load_json_ref
from agent_training.uve.export_training import apply_secret_placeholders


POLICY_REF = "config/uve/uve_receipt_explanation_manifest.json"


def _load_policy() -> dict:
    policy = load_json_ref(POLICY_REF)
    policy["_secret_policy"] = load_json_ref(policy["secret_policy_ref"])
    return policy


def _value(value: Any, policy: dict) -> Any:
    if isinstance(value, str):
        text = apply_secret_placeholders(value, policy["_secret_policy"])
        limit = policy["limits"]["text_characters"]
        return text if len(text) <= limit else text[:limit] + "…"
    if type(value) in (bool, int, float):
        return value
    if isinstance(value, list):
        return [_value(item, policy) for item in value[:policy["limits"]["items"]]
                if type(item) in (str, bool, int, float)]
    return None


def _project(source: dict, fields: list[str], policy: dict) -> dict:
    result = {}
    for field in fields:
        if (value := _value(source.get(field), policy)) is not None:
            result[field] = value
    return result


def explain_probe(receipt: dict, *, profile: dict | None = None,
                  mode: str = "adapter", elapsed_ms: int | None = None,
                  adapter_verdict: str | None = None, reason_code: str | None = None) -> dict:
    policy = _load_policy()
    probe = receipt["probe"]
    observation = receipt["observation"]
    expectation = receipt["expectation"]
    verdict = receipt["verdict"]
    scope = {**policy["defaults"], **policy["adapters"].get(probe["adapter_id"], {})}
    outcome = policy["probe_outcomes"][verdict]
    reason_code = reason_code or receipt.get("failure_class") or verdict
    catalog = load_json_ref(policy["failure_catalog_ref"])["classes"]
    definition = catalog.get(receipt.get("failure_class"), {}).get("description")
    reason = policy["probe_reasons"].get(reason_code) or definition or outcome
    facts = _project(observation, policy["observation_fields"], policy)
    flow_triage = observation.get("triage") or {}
    primary = flow_triage.get("primary") or {}
    checks = []
    total_checks = 0
    for name in policy["check_collections"]:
        rows = observation.get(name)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            total_checks += 1
            if len(checks) < policy["limits"]["items"]:
                checks.append({"ref": f"/observation/{name}/{index}",
                               **_project(row, policy["check_fields"], policy)})
    targets = {}
    for name in scope.get("target_fields", []):
        ref = (profile or {}).get(name)
        if isinstance(ref, dict):
            ref = ref.get(probe["probe_id"])
        if isinstance(ref, str):
            targets[name] = _value(ref, policy)
    what = {
        "probe_id": probe["probe_id"],
        "project_id": receipt["subject_identity"].get("project_id"),
        "expectation": expectation.get("outcome_token"),
        "expectation_ref": expectation.get("_manifest_path"),
        "subject_identity_sha256": receipt["subject_identity"].get("identity_sha256"),
    }
    detail = _value(observation.get("reason"), policy)
    summary = f"{probe['probe_id']} · {verdict}. {reason}"
    if facts.get("failed_step"):
        summary += f" Step: {facts['failed_step']}."
    if primary:
        finding = _project(primary, policy["finding_fields"], policy)
        finding["ref"] = "/observation/triage/primary"
    else:
        finding = None
    how = {
        "adapter_id": probe["adapter_id"], "mode": mode,
        "description": policy["blocked_method"] if mode == "lease_denied" else scope["method"],
        "profile_ref": _value(receipt.get("profile_ref"), policy), "target_refs": targets,
        "elapsed_ms": elapsed_ms, "adapter_verdict": adapter_verdict,
        "identity_stable": receipt["toctou"].get("identity_stable"),
    }
    return {
        "schema_version": policy["explanation_schema_version"], "policy_sha256": policy["_content_hash"],
        "summary": summary, "what": what, "how": how,
        "why": {
            "code": reason_code, "description": reason, "reported_reason": detail,
            "fault_origin": receipt["fault_origin"], "failure_class": receipt.get("failure_class"),
            "fault_rule": facts.get("fault_rule"),
            "reported_reason_ref": "/observation/reason" if detail is not None else None,
            "error_ref": "/observation/error" if "error" in observation else None,
            "primary_flow_finding": finding,
        },
        "what_it_shows": {
            "finding": outcome, "scope": scope["scope"],
            "limit": policy["fixture_limit"] if mode == "fixture" else scope["limit"],
        },
        "evidence": {
            "facts": facts, "fact_source": "/observation", "checks": checks,
            "omitted_checks": total_checks - len(checks),
            "flow_findings_count": len(flow_triage.get("findings", [])),
            "selector_ref": "/observation/missing_selector" if "missing_selector" in observation else None,
            "artifact_count": len(receipt.get("artifacts", [])),
            "artifacts_ref": "/artifacts" if receipt.get("artifacts") else None,
        },
    }


def probe_identifier(receipt: dict) -> str | None:
    descriptor = receipt.get("probe")
    value = descriptor.get("probe_id") if isinstance(descriptor, dict) else None
    return value if isinstance(value, str) else None


def explain_gate(receipt: dict, probes: list[dict], *, reason_code: str,
                 spec: dict | None = None, blocking_probe_ids: list[str] | None = None,
                 details: dict | None = None) -> dict:
    policy = _load_policy()
    decision = receipt["decision"]
    required = (spec or {}).get("required_probe_ids", [])
    present = {probe_identifier(p) for p in probes}
    reason = policy["gate_reasons"][reason_code]
    limit = policy["limits"]["items"]
    evidence = []
    for probe in probes[:limit]:
        observation = probe.get("observation", {})
        observation = observation if isinstance(observation, dict) else {}
        projection = probe.get("explanation") or {}
        why = projection.get("why") if isinstance(projection, dict) else None
        evidence.append({
            "probe_id": probe_identifier(probe),
            "probe_run_id": probe.get("probe_run_id"),
            "verdict": _value(probe.get("verdict"), policy),
            "fault_origin": _value(probe.get("fault_origin"), policy),
            "failure_class": _value(probe.get("failure_class"), policy),
            "reason_code": _value(why.get("code"), policy) if isinstance(why, dict) else None,
            "observations": _project(observation, ["failed_step", "fault_rule", "missing_fields"], policy),
        })
    return {
        "schema_version": policy["explanation_schema_version"], "policy_sha256": policy["_content_hash"],
        "summary": f"{receipt['gate_id']} · {decision}. {reason}",
        "what": {
            "gate_id": receipt["gate_id"], "required_probe_ids": required,
            "subject_identity_sha256": receipt["subject_identity_sha256"],
        },
        "how": {
            "description": policy["gate_method"], "evaluator_ref": receipt["gate_evaluator_ref"],
            "manifest_bundle_sha256": receipt["manifest_bundle_sha256"],
            "requires_stable_identity": (spec or {}).get("require_identity_stable", False),
            "requires_flow_replay": (spec or {}).get("require_flow_replay", False),
        },
        "why": {
            "code": reason_code, "description": reason,
            "rejection_basis": receipt.get("rejection_basis"),
            "blocking_probe_ids": blocking_probe_ids or [],
            "missing_probe_ids": [probe_id for probe_id in required if probe_id not in present],
            "details": _project(details, list(details), policy) if details else {},
        },
        "what_it_shows": {
            "finding": policy["gate_outcomes"][decision],
            "scope": receipt["gate_id"], "limit": policy["gate_limit"],
        },
        "evidence": {"probes": evidence, "probes_ref": "/probe_receipt_refs",
                     "omitted_probes": max(0, len(probes) - len(evidence))},
    }
