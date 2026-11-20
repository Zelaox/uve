#!/usr/bin/env python3
"""Read-only Catacombs triage: binding evidence, optional preflight, UVE incident bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[2]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from mcp_server.diagnostics.lab_uve_bridge import (  # noqa: E402
    load_bridge_manifest,
    load_ref,
    resolve_report_root,
)
from mcp_server.diagnostics.lab_uve_classification import (  # noqa: E402
    map_lab_finding,
    recommended_steps,
)
from mcp_server.diagnostics.lab_uve_incident_bundle import (  # noqa: E402
    build_incident_bundle,
    write_incident_bundle,
)
from mcp_server.diagnostics.ruined_catacombs_uve_validate import (  # noqa: E402
    load_catacombs_binding,
    resolve_catacombs_root,
    run_catacombs_uve_validation,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binding_path(lab_root: Path) -> Path:
    bridge = json.loads((lab_root / "config" / "lab_uve_triage_bridge_manifest.json").read_text(encoding="utf-8"))
    binding_ref = load_ref(lab_root, bridge["uve_binding_ref"])
    rel = str(binding_ref.get("catacombs_binding_ref") or "config/lab_uve_catacombs_binding.json")
    return lab_root / rel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="Run full Catacombs UVE validate (may take minutes)")
    parser.add_argument("--write-bundle", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--probe-run-id", default="")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    binding = load_catacombs_binding(LAB_ROOT)
    catacombs_root = resolve_catacombs_root(LAB_ROOT, binding)
    manifest_path = catacombs_root / str(
        binding.get("verification_manifest_relative_from_catacombs") or "config/uve/verification_manifest.json"
    )

    lab_findings: list[dict[str, Any]] = []
    preflight: dict[str, Any] | None = None
    if args.preflight:
        preflight = run_catacombs_uve_validation(LAB_ROOT)
        if not preflight.get("ok"):
            failed = preflight.get("failed_at") or {}
            lab_findings.append(
                {
                    "code": "CATACOMBS_CONTRACT_FAIL",
                    "certainty": "fact",
                    "detail": f"{failed.get('kind')}:{failed.get('id')}",
                }
            )

    primary_code = lab_findings[0]["code"] if lab_findings else "UNCLASSIFIED"
    mapping = map_lab_finding(LAB_ROOT, primary_code)
    step_ids = [str(mapping.get("next_step_id") or "triage_ruined_catacombs"), "uve_ruined_catacombs_validate"]
    recommended = recommended_steps(LAB_ROOT, step_ids)

    report: dict[str, Any] = {
        "schema_version": "triage_ruined_catacombs_session.v1",
        "generated_at": _utc_now(),
        "project_id": binding.get("project_id"),
        "catacombs_root": str(catacombs_root),
        "verification_manifest": str(manifest_path.relative_to(catacombs_root)).replace("\\", "/"),
        "lab_findings": lab_findings,
        "mapping": mapping,
        "recommended_next": recommended,
        "uve_profile_relative_in_pipeline": binding.get("uve_profile_relative_in_pipeline"),
        "uve_gate_id": binding.get("uve_gate_id"),
        "preflight": preflight,
    }

    if args.write_report or args.json_out:
        bridge = load_bridge_manifest(LAB_ROOT)
        out = args.json_out or (
            resolve_report_root(LAB_ROOT, bridge)
            / f"triage_ruined_catacombs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["report_path"] = str(out)

    if args.write_bundle:
        bundle = build_incident_bundle(
            repo_root=LAB_ROOT,
            probe_run_id=args.probe_run_id,
        )
        bundle["lab_findings"] = lab_findings + list(bundle.get("lab_findings") or [])
        bundle["recommended_next"] = recommended
        bundle["catacombs"] = {
            "binding_ref": str(_binding_path(LAB_ROOT).relative_to(LAB_ROOT)).replace("\\", "/"),
            "catacombs_root": str(catacombs_root),
            "primary_finding_code": primary_code,
        }
        paths = write_incident_bundle(repo_root=LAB_ROOT, bundle=bundle)
        report["incident_bundle"] = {k: str(v) for k, v in paths.items()}

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if lab_findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
