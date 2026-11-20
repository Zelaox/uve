"""Read-only correlation and classification for pipeline incidents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return rows


def _legacy_classify(events: Iterable[dict[str, Any]], contracts: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = list(events)
    stages = [str(row.get("stage") or row.get("event_type") or "") for row in rows]
    findings: list[dict[str, str]] = []
    image_selected = any(
        stage in {"cip_intent_classification", "turn_dispatch_contract_compiled"}
        and ((row.get("data") or {}).get("intent") == "image_generation"
             or (row.get("data") or {}).get("selected_pipeline") == "image_generation")
        for stage, row in zip(stages, rows)
    )
    code_override = any(stage == "cip_work_mode_override" for stage in stages)
    if image_selected and code_override:
        findings.append({"code": "DISPATCH_INTENT_CONTRADICTION", "certainty": "fact",
                         "detail": "Image generation was selected before a code/work-mode override."})
    operation_keys: set[tuple[str, str]] = set()
    for row in rows:
        data = row.get("data") or {}
        operation = row.get("operation") or {}
        key = (str(row.get("stage") or row.get("event_type") or ""),
               str(data.get("operation_id") or row.get("operation_id")
                   or operation.get("operation_id") or operation.get("projection_id") or ""))
        if key[1] and key in operation_keys:
            findings.append({"code": "DUPLICATE_OPERATION_PROJECTION", "certainty": "fact",
                             "detail": f"Operation {key[1]} was projected more than once."})
            break
        operation_keys.add(key)
    terminal = any(stage in {"turn_failed", "turn_closed", "pipeline_turn_summary"} for stage in stages)
    if rows and not terminal:
        findings.append({"code": "OBSERVABILITY_EVIDENCE_GAP", "certainty": "unknown",
                         "detail": "Events exist, but no terminal turn event was found."})
    if image_selected and not contracts:
        findings.append({"code": "CONTRACT_CREATION_MISSING", "certainty": "fact",
                         "detail": "Image intent exists without a correlated execution contract."})
    return findings


def _classify(events: Iterable[dict[str, Any]], contracts: list[dict[str, Any]], *, root: Path) -> list[dict[str, str]]:
    bridge_path = root / "config" / "lab_uve_triage_bridge_manifest.json"
    if not bridge_path.is_file():
        return _legacy_classify(events, contracts)
    from mcp_server.diagnostics.lab_uve_classification import classify_incident_events

    return classify_incident_events(events, contracts, repo_root=root)


def triage_turn(turn_id: str, *, root: Path) -> dict[str, Any]:
    """Correlate one turn without writing, retrying, or repairing anything."""
    root = Path(root).resolve()
    events: list[dict[str, Any]] = []
    direct = root / "data" / "runtime" / "turn_events" / f"{turn_id}.jsonl"
    events.extend(_jsonl(direct))
    daily = root / "data" / "memory" / "layer_1_daily"
    for path in sorted(daily.glob("events_*.jsonl")) if daily.is_dir() else []:
        events.extend(row for row in _jsonl(path) if str(row.get("turn_id") or "") == turn_id)
    contracts: list[dict[str, Any]] = []
    contract_dir = root / "data" / "runtime" / "execution_contracts"
    for path in sorted(contract_dir.glob("*.json")) if contract_dir.is_dir() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            intent = value.get("intent") or {}
            if str(intent.get("turn_id") or value.get("parent_id") or "") == turn_id:
                contracts.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    events.sort(key=lambda row: str(row.get("timestamp") or ""))
    return {"schema_version": 1, "mode": "read_only", "turn_id": turn_id,
            "facts": {"event_count": len(events), "contract_count": len(contracts),
                      "first_stage": str(events[0].get("stage") or "") if events else "",
                      "last_stage": str(events[-1].get("stage") or "") if events else ""},
            "findings": _classify(events, contracts, root=root), "events": events, "contracts": contracts}
