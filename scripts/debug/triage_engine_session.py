#!/usr/bin/env python3
"""Read-only AI Lab engine triage: runtime advisory, optional UVE preflight, incident bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[2]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from mcp_server.diagnostics.engine_uve_validate import (  # noqa: E402
    load_engine_binding,
    resolve_engine_root,
    run_engine_uve_validation,
)
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_triage_policy(lab_root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    rel = str(binding.get("engine_triage_policy_relative_from_lab") or "config/engine_triage_policy.json")
    return _load_json(lab_root / rel)


def _load_session_report_policy(
    lab_root: Path, binding: dict[str, Any], triage_policy: dict[str, Any]
) -> dict[str, Any]:
    ref = str(
        triage_policy.get("triage_session_report_ref")
        or binding.get("engine_triage_session_report_relative_from_lab")
        or "config/engine_triage_session_report.json"
    )
    path = lab_root / ref
    if not path.is_file():
        return {}
    return _load_json(path)


def _triage_codes_for_advisory(
    lab_findings: list[dict[str, Any]], runtime_report: dict[str, Any] | None
) -> list[str]:
    codes = [str(f.get("code") or "") for f in lab_findings if f.get("code")]
    if runtime_report:
        for item in runtime_report.get("findings") or []:
            code = str(item.get("diagnostic_code") or "")
            if code and code not in codes:
                codes.append(code)
    return codes


def _write_report_file(lab_root: Path, report: dict[str, Any], json_out: Path | None) -> None:
    bridge = load_bridge_manifest(lab_root)
    out = json_out or (
        resolve_report_root(lab_root, bridge)
        / f"triage_engine_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(out)


def _binding_path(lab_root: Path) -> Path:
    bridge = json.loads((lab_root / "config" / "lab_uve_triage_bridge_manifest.json").read_text(encoding="utf-8"))
    binding_ref = load_ref(lab_root, bridge["uve_binding_ref"])
    rel = str(binding_ref.get("engine_binding_ref") or "config/lab_uve_engine_binding.json")
    return lab_root / rel


def _runtime_triage_findings(
    lab_root: Path,
    binding: dict[str, Any],
    *,
    runtime_jsonl: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    script_rel = str(binding.get("runtime_triage_script_relative_from_lab") or "scripts/engine/triage_runtime.py")
    script = lab_root / script_rel
    policy_rel = str(binding.get("engine_triage_policy_relative_from_lab") or "config/engine_triage_policy.json")
    command = [sys.executable, str(script), "--policy", policy_rel]
    if runtime_jsonl:
        command.extend(["--runtime-jsonl", runtime_jsonl])
    proc = subprocess.run(
        command,
        cwd=str(lab_root),
        capture_output=True,
        text=True,
        check=False,
    )
    policy = json.loads((lab_root / policy_rel).read_text(encoding="utf-8"))
    report_dir = lab_root / policy.get("report_dir", "logs/engine/triage")
    reports = sorted(report_dir.glob("report_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    runtime_report: dict[str, Any] | None = None
    if reports:
        runtime_report = json.loads(reports[0].read_text(encoding="utf-8"))

    findings: list[dict[str, Any]] = []
    if proc.returncode != 0:
        findings.append(
            {
                "code": "ENGINE_RUNTIME_TRIAGE_FAIL",
                "certainty": "fact",
                "detail": (proc.stderr or proc.stdout or "runtime triage failed").strip()[:240],
            }
        )
    elif runtime_report:
        schema_status = str(runtime_report.get("schema_validation_status") or "ok")
        if schema_status == "fail":
            findings.append(
                {
                    "code": "ENGINE_RUNTIME_SCHEMA_FAIL",
                    "certainty": "fact",
                    "detail": f"blocking_schema_issues={runtime_report.get('schema_blocking_issue_count', 0)}",
                }
            )
        for item in runtime_report.get("findings") or []:
            code = str(item.get("diagnostic_code") or "ENGINE_RUNTIME_ADVISORY")
            findings.append(
                {
                    "code": code,
                    "certainty": "hypothesis",
                    "detail": str(item.get("recommendation") or item.get("diagnostic_code") or ""),
                }
            )
    return findings, runtime_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="Run full engine UVE validate (may take minutes)")
    parser.add_argument("--runtime-triage", action="store_true", help="Run scripts/engine/triage_runtime.py")
    parser.add_argument("--write-bundle", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--index", action="store_true", help="Write a report and invoke the configured index sidecar")
    parser.add_argument("--probe-run-id", default="")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument(
        "--expansion-advisory",
        action="store_true",
        help="After triage, run Test Expansion Advisor (advisory; does not change UVE token)",
    )
    parser.add_argument(
        "--runtime-jsonl",
        default=None,
        help="Override runtime JSONL for triage_runtime.py (absolute or relative to lab root)",
    )
    parser.add_argument(
        "--no-expansion-enrichment",
        action="store_true",
        help="Skip policy-driven expansion advisory/evolution blocks on --write-report",
    )
    args = parser.parse_args()

    binding = load_engine_binding(LAB_ROOT)
    triage_policy = _load_triage_policy(LAB_ROOT, binding)
    session_report_policy = _load_session_report_policy(LAB_ROOT, binding, triage_policy)
    engine_root = resolve_engine_root(LAB_ROOT, binding)
    manifest_path = engine_root / str(
        binding.get("verification_manifest_relative_from_engine") or "config/uve/verification_manifest.json"
    )

    lab_findings: list[dict[str, Any]] = []
    preflight: dict[str, Any] | None = None
    runtime_report: dict[str, Any] | None = None

    if args.runtime_triage:
        runtime_findings, runtime_report = _runtime_triage_findings(
            LAB_ROOT, binding, runtime_jsonl=args.runtime_jsonl
        )
        lab_findings.extend(runtime_findings)

    if args.preflight:
        preflight = run_engine_uve_validation(LAB_ROOT)
        if not preflight.get("ok"):
            failed = preflight.get("failed_at") or {}
            lab_findings.append(
                {
                    "code": "ENGINE_CONTRACT_FAIL",
                    "certainty": "fact",
                    "detail": f"{failed.get('kind')}:{failed.get('id')}",
                }
            )

    blocking = [
        f
        for f in lab_findings
        if f.get("code")
        in {"ENGINE_CONTRACT_FAIL", "ENGINE_RUNTIME_TRIAGE_FAIL", "ENGINE_RUNTIME_SCHEMA_FAIL"}
    ]
    primary_code = blocking[0]["code"] if blocking else (lab_findings[0]["code"] if lab_findings else "UNCLASSIFIED")
    mapping = map_lab_finding(LAB_ROOT, primary_code)
    step_ids = [
        str(mapping.get("next_step_id") or "triage_engine_session"),
        str(binding.get("bundle_outcome_step_id") or "uve_ailab_engine_validate"),
    ]
    recommended = recommended_steps(LAB_ROOT, step_ids)

    report: dict[str, Any] = {
        "schema_version": "triage_engine_session.v1",
        "generated_at": _utc_now(),
        "project_id": binding.get("project_id"),
        "engine_root": str(engine_root),
        "verification_manifest": str(manifest_path.relative_to(engine_root)).replace("\\", "/"),
        "lab_findings": lab_findings,
        "mapping": mapping,
        "recommended_next": recommended,
        "uve_profile_relative_in_pipeline": binding.get("uve_profile_relative_in_pipeline"),
        "uve_gate_id": binding.get("uve_gate_id"),
        "preflight": preflight,
        "runtime_triage": runtime_report,
        "triage_session_report_policy_ref": triage_policy.get("triage_session_report_ref"),
    }

    if runtime_report and (session_report_policy.get("include_on_write_report") or {}).get(
        "runtime_triage_counters", False
    ):
        report["runtime_triage_summary"] = {
            "records_scanned": runtime_report.get("records_scanned"),
            "schema_validation_status": runtime_report.get("schema_validation_status"),
            "schema_unknown_event_count": runtime_report.get("schema_unknown_event_count"),
            "schema_blocking_issue_count": runtime_report.get("schema_blocking_issue_count"),
            "finding_counts_by_code": runtime_report.get("finding_counts_by_code"),
            "finding_total": len(runtime_report.get("findings") or []),
        }

    want_advisory = args.expansion_advisory
    include_cfg = session_report_policy.get("include_on_write_report") or {}
    if not args.no_expansion_enrichment and (args.write_report or args.json_out):
        want_advisory = want_advisory or bool(include_cfg.get("expansion_advisory"))

    if want_advisory:
        from mcp_server.diagnostics.uve_test_expansion_advisor import run_test_expansion_advisory

        report["expansion_advisory"] = run_test_expansion_advisory(
            LAB_ROOT,
            triage_codes=_triage_codes_for_advisory(lab_findings, runtime_report),
        )

    if not args.no_expansion_enrichment and (args.write_report or args.json_out):
        if include_cfg.get("expansion_evolution_snapshot"):
            from mcp_server.diagnostics.uve_expansion_triage_snapshot import load_expansion_triage_snapshot

            report["expansion_evolution_snapshot"] = load_expansion_triage_snapshot(LAB_ROOT)

    if args.write_bundle:
        bundle = build_incident_bundle(
            repo_root=LAB_ROOT,
            probe_run_id=args.probe_run_id,
        )
        bundle["lab_findings"] = lab_findings + list(bundle.get("lab_findings") or [])
        bundle["recommended_next"] = recommended
        bundle["ailab_engine"] = {
            "binding_ref": str(_binding_path(LAB_ROOT).relative_to(LAB_ROOT)).replace("\\", "/"),
            "engine_root": str(engine_root),
            "primary_finding_code": primary_code,
        }
        paths = write_incident_bundle(repo_root=LAB_ROOT, bundle=bundle)
        report["incident_bundle"] = {k: str(v) for k, v in paths.items()}

    if args.write_report or args.json_out or args.index:
        _write_report_file(LAB_ROOT, report, args.json_out)

    if args.index:
        from mcp_server.diagnostics.uve_triage_adapter import index_sidecar

        report["observability_index"] = index_sidecar(LAB_ROOT, "engine_triage_index", Path(report["report_path"]))

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if blocking:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
