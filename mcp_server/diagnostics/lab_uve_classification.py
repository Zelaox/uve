"""Manifest-driven incident classification and Lab→UVE mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from mcp_server.diagnostics.lab_uve_bridge import load_bridge_manifest, load_ref
from mcp_server.diagnostics.lab_uve_hash import structural_hash


def _image_selected(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        stage = str(row.get("stage") or row.get("event_type") or "")
        if stage != "cip_intent_classification":
            continue
        data = row.get("data") or {}
        if data.get("intent") == "image_generation" or data.get("selected_pipeline") == "image_generation":
            return True
    return False


def _code_override(rows: list[dict[str, Any]]) -> bool:
    return any(str(row.get("stage") or row.get("event_type") or "") == "cip_work_mode_override" for row in rows)


def _duplicate_operation(rows: list[dict[str, Any]]) -> bool:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        data = row.get("data") or {}
        operation = row.get("operation") or {}
        key = (
            str(row.get("stage") or row.get("event_type") or ""),
            str(
                data.get("operation_id")
                or row.get("operation_id")
                or operation.get("operation_id")
                or operation.get("projection_id")
                or ""
            ),
        )
        if key[1] and key in keys:
            return True
        keys.add(key)
    return False


def _terminal_missing(rows: list[dict[str, Any]]) -> bool:
    stages = [str(row.get("stage") or row.get("event_type") or "") for row in rows]
    terminal = any(stage in {"turn_failed", "turn_closed", "pipeline_turn_summary"} for stage in stages)
    return bool(rows) and not terminal


def classify_incident_events(
    events: Iterable[dict[str, Any]],
    contracts: list[dict[str, Any]],
    *,
    repo_root: Path,
) -> list[dict[str, str]]:
    rows = list(events)
    bridge = load_bridge_manifest(repo_root)
    manifest = load_ref(repo_root, bridge["incident_classifier_manifest_ref"])
    findings: list[dict[str, str]] = []
    flags = {
        "intent_image": _image_selected(rows),
        "code_override": _code_override(rows),
        "duplicate_operation": _duplicate_operation(rows),
        "events_without_terminal": _terminal_missing(rows),
        "image_without_contract": _image_selected(rows) and not contracts,
    }
    for rule in manifest.get("rules") or []:
        match = rule.get("match") or {}
        ok = True
        if match.get("requires_stages_any") and not flags["intent_image"]:
            ok = False
        if match.get("intent_image") and not flags["intent_image"]:
            ok = False
        if match.get("requires_stages_any_after") and not flags["code_override"]:
            ok = False
        if match.get("duplicate_operation") and not flags["duplicate_operation"]:
            ok = False
        if match.get("events_without_terminal") and not flags["events_without_terminal"]:
            ok = False
        if match.get("image_without_contract") and not flags["image_without_contract"]:
            ok = False
        if ok:
            findings.append(
                {
                    "code": str(rule.get("finding_code") or ""),
                    "certainty": str(rule.get("certainty") or "unknown"),
                    "detail": str(rule.get("detail_template") or ""),
                }
            )
    return findings


def primary_finding_code(findings: list[dict[str, str]], *, repo_root: Path) -> str:
    if findings:
        return str(findings[0].get("code") or "")
    bridge = load_bridge_manifest(repo_root)
    identity = load_ref(repo_root, bridge["incident_identity_manifest_ref"])
    return str(identity.get("default_primary_finding_code") or "UNCLASSIFIED")


def map_lab_finding(repo_root: Path, finding_code: str) -> dict[str, Any]:
    bridge = load_bridge_manifest(repo_root)
    cmap = load_ref(repo_root, bridge["classification_map_ref"])
    for row in cmap.get("mappings") or []:
        if row.get("lab_finding_code") == finding_code:
            return dict(row)
    for row in cmap.get("mappings") or []:
        if row.get("lab_finding_code") == "UNCLASSIFIED":
            return dict(row)
    return {"lab_finding_code": finding_code, "uve_failure_class": "not_verified"}


def recommended_steps(repo_root: Path, step_ids: list[str]) -> list[dict[str, Any]]:
    bridge = load_bridge_manifest(repo_root)
    catalog = load_ref(repo_root, bridge["next_step_catalog_ref"])
    by_id = {str(s.get("step_id")): s for s in catalog.get("steps") or []}
    out: list[dict[str, Any]] = []
    for sid in step_ids:
        step = by_id.get(sid)
        if step:
            out.append(dict(step))
    return out


def logical_incident_id(repo_root: Path, fingerprint: dict[str, Any]) -> str:
    bridge = load_bridge_manifest(repo_root)
    identity = load_ref(repo_root, bridge["incident_identity_manifest_ref"])
    fields = identity.get("logical_fingerprint_fields") or []
    payload = {k: fingerprint.get(k) for k in fields}
    return structural_hash(payload)


def occurrence_id(repo_root: Path, *, logical_id: str, evidence_epoch: str, bundle_build_id: str) -> str:
    bridge = load_bridge_manifest(repo_root)
    identity = load_ref(repo_root, bridge["incident_identity_manifest_ref"])
    fields = identity.get("occurrence_fields") or []
    payload = {
        "logical_incident_id": logical_id,
        "evidence_epoch": evidence_epoch,
        "bundle_build_id": bundle_build_id,
    }
  # only use declared fields
    trimmed = {k: payload[k] for k in fields if k in payload}
    return structural_hash(trimmed or payload)
