from __future__ import annotations

import json
from pathlib import Path

from mcp_server.diagnostics.lab_uve_bundle_validate import validate_bundle_dict
from mcp_server.diagnostics.lab_uve_classification import logical_incident_id, occurrence_id
from mcp_server.diagnostics.lab_uve_incident_bundle import build_incident_bundle, write_incident_bundle


def test_logical_and_occurrence_ids_differ_on_new_epoch():
    root = Path(__file__).resolve().parents[2]
    fp = {"turn_id": "t1", "plan_slug": "slug_a", "primary_finding_code": "UNCLASSIFIED"}
    logical = logical_incident_id(root, fp)
    occ_a = occurrence_id(root, logical_id=logical, evidence_epoch="epoch-a", bundle_build_id="b1")
    occ_b = occurrence_id(root, logical_id=logical, evidence_epoch="epoch-b", bundle_build_id="b2")
    assert logical
    assert occ_a != occ_b


def test_bundle_write_and_validate(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    run_dir = tmp_path / "data" / "work_runs"
    run_dir.mkdir(parents=True)
    run_path = run_dir / "run_test.json"
    run_path.write_text(
        json.dumps({"turn_id": "", "plan_slug": "demo_slug", "failure_reason": "CODEGEN_STALL"}),
        encoding="utf-8",
    )
    bundle = build_incident_bundle(repo_root=root, run_path=run_path)
    assert bundle["schema_version"] == "lab_uve_incident_bundle.v1"
    assert bundle["primary_finding_code"] == "CODEGEN_STALL"
    from mcp_server.diagnostics.lab_uve_bridge import load_bridge_manifest, resolve_report_root

    report_root = resolve_report_root(root, load_bridge_manifest(root))
    out_dir = report_root / "pytest_fixture" / bundle["occurrence_id"]
    paths = write_incident_bundle(repo_root=root, bundle=bundle, output_dir=out_dir)
    assert paths["bundle_path"].is_file()
    validate_bundle_dict(root, json.loads(paths["bundle_path"].read_text(encoding="utf-8")))
