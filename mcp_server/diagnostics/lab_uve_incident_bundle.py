"""Build read-only Lab ↔ UVE incident bundles (writes only under report catalog)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp_server.diagnostics.incident_triage import triage_turn
from mcp_server.diagnostics.lab_uve_bridge import (
    assert_write_allowed,
    load_bridge_manifest,
    load_ref,
    resolve_report_root,
)
from mcp_server.diagnostics.lab_uve_classification import (
    classify_incident_events,
    logical_incident_id,
    map_lab_finding,
    occurrence_id,
    primary_finding_code,
    recommended_steps,
)
from mcp_server.diagnostics.lab_uve_hash import file_content_sha256, structural_hash


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_run(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _trust_for_path(repo_root: Path, bridge: dict[str, Any], rel_path: str) -> dict[str, Any]:
    catalog = load_ref(repo_root, bridge["evidence_trust_catalog_ref"])
    auth_roots = [str(r) for r in catalog.get("authoritative_roots_relative") or []]
    read_roots = [str(r) for r in catalog.get("allowed_read_roots_relative") or []]
    rel = rel_path.replace("\\", "/").lstrip("/")
    full = repo_root / rel
    row: dict[str, Any] = {
        "ref": rel,
        "authority_ref": catalog.get("default_authority_ref"),
        "trust_decision_ref": "path_root_rules",
    }
    if not full.is_file():
        row["trust_tier"] = "unrecoverable"
        return row
    row["content_sha256"] = file_content_sha256(full)
    normalized = rel
    tier = "external_untrusted"
    for root in auth_roots:
        if normalized == root or normalized.startswith(root.rstrip("/") + "/"):
            tier = "authoritative"
            break
    if tier == "external_untrusted":
        for root in read_roots:
            if normalized == root or normalized.startswith(root.rstrip("/") + "/"):
                tier = "derived"
                break
    row["trust_tier"] = tier
    return row


def build_incident_bundle(
    *,
    repo_root: Path,
    turn_id: str = "",
    run_path: Optional[Path] = None,
    probe_run_id: str = "",
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    bridge = load_bridge_manifest(repo_root)
    run_data: dict[str, Any] = {}
    correlation: dict[str, Any] = {}
    if run_path and run_path.is_file():
        run_data = _load_run(run_path)
        correlation["run_id"] = run_path.stem
        try:
            correlation["run_path_relative"] = str(run_path.resolve().relative_to(repo_root)).replace("\\", "/")
        except ValueError:
            correlation["run_path_external"] = str(run_path.resolve())
    correlation["turn_id"] = turn_id or str(run_data.get("turn_id") or "")
    correlation["plan_slug"] = str(run_data.get("plan_slug") or "")
    correlation["probe_run_id"] = probe_run_id or str(run_data.get("probe_run_id") or "")

    turn_findings: list[dict[str, str]] = []
    lab_findings: list[dict[str, Any]] = []
    if correlation["turn_id"]:
        turn_report = triage_turn(correlation["turn_id"], root=repo_root)
        turn_findings = list(turn_report.get("findings") or [])
        lab_findings.extend(turn_findings)
    failure_reason = str(run_data.get("failure_reason") or "")
    if failure_reason and not any(f.get("code") == failure_reason for f in lab_findings):
        lab_findings.append({"code": failure_reason, "certainty": "fact", "detail": "work_run.failure_reason"})

    primary_code = primary_finding_code(lab_findings, repo_root=repo_root)
    mapping = map_lab_finding(repo_root, primary_code)
    step_id = str(mapping.get("next_step_id") or "triage_debug_first")
    recommended = recommended_steps(repo_root, [step_id, "uve_bundle_validate"])

    fingerprint = {
        "turn_id": correlation.get("turn_id"),
        "plan_slug": correlation.get("plan_slug"),
        "primary_finding_code": primary_code,
    }
    logical_id = logical_incident_id(repo_root, fingerprint)
    epoch_payload = {
        "correlation": correlation,
        "primary_finding_code": primary_code,
        "lab_findings": lab_findings,
    }
    evidence_epoch = structural_hash(epoch_payload)
    bundle_build_id = str(uuid.uuid4())
    occ_id = occurrence_id(
        repo_root,
        logical_id=logical_id,
        evidence_epoch=evidence_epoch,
        bundle_build_id=bundle_build_id,
    )

    evidence_refs: list[dict[str, Any]] = []
    gaps: list[str] = []
    if run_path and run_path.is_file():
        rel = correlation.get("run_path_relative")
        if rel:
            evidence_refs.append(_trust_for_path(repo_root, bridge, rel))
        else:
            evidence_refs.append(
                {
                    "ref": correlation.get("run_path_external"),
                    "trust_tier": "external_untrusted",
                    "authority_ref": load_ref(repo_root, bridge["evidence_trust_catalog_ref"]).get(
                        "default_authority_ref"
                    ),
                    "trust_decision_ref": "external_run_path",
                    "content_sha256": file_content_sha256(run_path),
                }
            )
    if not correlation.get("turn_id") and not run_path:
        gaps.append("missing_correlation_ids")

    bundle = {
        "schema_version": bridge.get("bundle_schema_version"),
        "mode": "read_only_sources",
        "created_at": _utc_now(),
        "logical_incident_id": logical_id,
        "occurrence_id": occ_id,
        "bundle_build_id": bundle_build_id,
        "evidence_epoch": evidence_epoch,
        "correlation": correlation,
        "lab_findings": lab_findings,
        "primary_finding_code": primary_code,
        "uve_classification": mapping,
        "evidence_trust": evidence_refs,
        "lab_evidence_envelopes": [],
        "uve_receipt_refs": [],
        "uve_triage_projection": None,
        "recommended_next": recommended,
        "gaps": gaps,
        "bridge_manifest_ref": bridge.get("bridge_manifest_path_relative"),
    }
    if probe_run_id:
        gaps.append("uve_triage_attach_pending_operator_flow_triage")
    return bundle


def render_summary(repo_root: Path, bundle: dict[str, Any]) -> str:
    bridge = load_bridge_manifest(repo_root)
    rel = bridge.get("summary_template_relative")
    if not rel:
        raise ValueError("summary_template_relative missing from bridge manifest")
    tpl = (repo_root / rel).read_text(encoding="utf-8")
    corr = bundle.get("correlation") or {}
    corr_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(corr.items()) if v)
    rec_lines = "\n".join(
        f"- {r.get('step_id')} ({r.get('kind')}) advisory={r.get('advisory_only')}"
        for r in bundle.get("recommended_next") or []
    )
    gap_lines = "\n".join(f"- {g}" for g in bundle.get("gaps") or []) or "(none)"
    return tpl.format(
        logical_incident_id=bundle.get("logical_incident_id"),
        occurrence_id=bundle.get("occurrence_id"),
        schema_version=bundle.get("schema_version"),
        correlation_block=corr_lines or "(empty)",
        primary_finding=bundle.get("primary_finding_code"),
        recommended_next_block=rec_lines or "(none)",
        gaps_block=gap_lines,
    )


def write_incident_bundle(
    *,
    repo_root: Path,
    bundle: dict[str, Any],
    output_dir: Optional[Path] = None,
) -> dict[str, Path]:
    repo_root = repo_root.resolve()
    bridge = load_bridge_manifest(repo_root)
    report_root = resolve_report_root(repo_root, bridge)
    logical = str(bundle.get("logical_incident_id"))
    occ = str(bundle.get("occurrence_id"))
    target_dir = output_dir or (report_root / logical / occ)
    bundle_path = target_dir / "bundle.json"
    summary_path = target_dir / "summary.md"
    assert_write_allowed(repo_root, bridge, bundle_path)
    assert_write_allowed(repo_root, bridge, summary_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(bundle, indent=2, sort_keys=True, default=str)
    budget = load_ref(repo_root, bridge["evidence_budget_ref"])
    max_bytes = int(budget.get("max_bundle_json_bytes") or 0)
    if max_bytes and len(payload.encode("utf-8")) > max_bytes:
        raise ValueError("bundle exceeds evidence_budget max_bundle_json_bytes")
    bundle_path.write_text(payload + "\n", encoding="utf-8")
    summary_path.write_text(render_summary(repo_root, bundle), encoding="utf-8")
    return {"bundle_path": bundle_path, "summary_path": summary_path}
