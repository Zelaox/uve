"""Format training-pipeline triage signals from incident bundles."""

from __future__ import annotations

from typing import Any

from mcp_server.diagnostics.lab_uve_bridge import load_bridge_manifest


def bundle_signal(bundle: dict[str, Any], *, repo_root) -> dict[str, Any]:
    bridge = load_bridge_manifest(repo_root)
    schema = bridge.get("bundle_schema_version")
    return {
        "signal_source_ref": "lab.incident_bundle",
        "bundle_schema": schema,
        "logical_incident_id": bundle.get("logical_incident_id"),
        "occurrence_id": bundle.get("occurrence_id"),
        "primary_finding_code": bundle.get("primary_finding_code"),
        "title": bundle.get("primary_finding_code"),
        "summary": str(bundle.get("correlation")),
        "artifact_hashes": [
            r.get("content_sha256")
            for r in bundle.get("evidence_trust") or []
            if r.get("content_sha256")
        ],
    }
