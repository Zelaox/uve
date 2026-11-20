from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server.diagnostics.lab_uve_bridge import (
    LabUveBridgeError,
    assert_write_allowed,
    load_bridge_manifest,
    resolve_report_root,
)


def test_bridge_manifest_loads_all_refs():
    root = Path(__file__).resolve().parents[2]
    bridge = load_bridge_manifest(root)
    assert bridge["schema_version"] == "lab_uve_triage_bridge_manifest.v1"
    report = resolve_report_root(root, bridge)
    assert report.is_dir() or not report.exists()


def test_write_outside_report_catalog_fails(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    bridge = load_bridge_manifest(root)
    forbidden = root / "data" / "work_runs" / "bundle.json"
    with pytest.raises(LabUveBridgeError):
        assert_write_allowed(root, bridge, forbidden)


def test_write_inside_report_catalog_allowed(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    bridge = load_bridge_manifest(root)
    report = resolve_report_root(root, bridge)
    target = report / "test_logical" / "test_occ" / "bundle.json"
    assert_write_allowed(root, bridge, target)


def test_missing_manifest_raises(tmp_path: Path):
    with pytest.raises(LabUveBridgeError):
        load_bridge_manifest(tmp_path)
