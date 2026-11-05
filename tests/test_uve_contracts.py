from __future__ import annotations

import json

import pytest

from agent_training.manifest_loader import pipeline_root
from agent_training.uve.bundle import load_manifest_bundle
from agent_training.uve.export_training import apply_secret_placeholders, derive_training_label
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.identity import build_inventory_records, workspace_files_inventory_sha256
from agent_training.uve.receipts import validate_probe_receipt


def test_manifest_bundle_loads():
    bundle = load_manifest_bundle()
    assert bundle["manifest_bundle_sha256"].startswith("sha256:")
    assert len(bundle["_loaded"]) >= 10


def test_probe_receipt_rejects_gate_fields():
    bad = {
        "schema_version": "uve_probe_receipt.v1",
        "gate_decision": "approved",
        "verdict": "passed",
        "fault_origin": "none",
        "subject_identity": {},
    }
    errs = validate_probe_receipt(bad)
    assert any("gate_decision" in e for e in errs)


def test_probe_rejects_git_oid():
    bad = {
        "schema_version": "uve_probe_receipt.v1",
        "git_commit_oid": "abc",
        "verdict": "passed",
        "fault_origin": "none",
        "subject_identity": {},
    }
    errs = validate_probe_receipt(bad)
    assert any("git" in e for e in errs)


def test_inventory_detects_rename(tmp_path):
    profile = {
        "project_id": "t",
        "environment": "development",
        "identity": {"watch_globs": ["*.txt"], "ignore_globs": []},
        "workspace_root_relative": ".",
    }
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same", encoding="utf-8")
    rec_a = build_inventory_records(tmp_path, profile)
    h_a = workspace_files_inventory_sha256(rec_a)
    a.unlink()
    b.write_text("same", encoding="utf-8")
    rec_b = build_inventory_records(tmp_path, profile)
    h_b = workspace_files_inventory_sha256(rec_b)
    assert h_a != h_b


def test_toctou_forces_not_verified_on_pass_with_unstable():
    bad = {
        "schema_version": "uve_probe_receipt.v1",
        "verdict": "passed",
        "fault_origin": "none",
        "subject_identity": {},
        "toctou": {"identity_stable": False},
    }
    errs = validate_probe_receipt(bad)
    assert any("identity_stable" in e for e in errs)


def test_verified_failure_requires_subject_observed():
    gate = {
        "decision": "rejected",
        "rejection_basis": "engine_fault",
        "subject_identity_sha256": "sha256:x",
    }
    probes = [
        {
            "probe": {"probe_id": "dry_echo"},
            "verdict": "failed",
            "fault_origin": "subject_fault",
            "toctou": {"identity_stable": True},
        }
    ]
    label = derive_training_label(gate, probes, ["dry_echo"])
    assert label == "unverified_proposal"


def test_verified_failure_label_when_subject_observed():
    gate = {
        "decision": "rejected",
        "rejection_basis": "subject_observed",
        "subject_identity_sha256": "sha256:x",
    }
    probes = [
        {
            "probe": {"probe_id": "dry_echo"},
            "verdict": "failed",
            "fault_origin": "subject_fault",
            "toctou": {"identity_stable": True},
        }
    ]
    label = derive_training_label(gate, probes, ["dry_echo"])
    assert label == "verified_failure"


def test_not_verified_probe_forbids_verified_failure():
    gate = {
        "decision": "rejected",
        "rejection_basis": "subject_observed",
        "subject_identity_sha256": "sha256:x",
    }
    probes = [
        {
            "probe": {"probe_id": "dry_echo"},
            "verdict": "not_verified",
            "fault_origin": "engine_fault",
            "toctou": {"identity_stable": True},
        }
    ]
    label = derive_training_label(gate, probes, ["dry_echo"])
    assert label == "unverified_proposal"


def test_secret_placeholder_bearer():
    raw = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    out = apply_secret_placeholders(raw, "config/uve/uve_secret_placeholder_manifest.json")
    assert "eyJ" not in out
    assert "<AUTH_TOKEN_1>" in out


def test_gate_approve_smoke_probe():
    profile_ref = "config/uve/profiles/emulator_workstation_smoke.json"
    from agent_training.uve.orchestrator import run_probe

    receipt = run_probe(profile_ref, "dry_echo")
    bundle = load_manifest_bundle()
    gate = evaluate_gate(
        "smoke_close_issue",
        [receipt],
        manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
        gate_evaluator_ref="pytest",
    )
    assert gate["decision"] == "approved"


def test_evidence_store_repair_agent_denied():
    from agent_training.manifest_loader import ManifestError
    from agent_training.uve.evidence_store import EvidenceStore

    store = EvidenceStore()
    store.write_raw("uve_runner", "secret.log", "Authorization: Bearer secretvalue")
    with pytest.raises(ManifestError):
        store.read_raw("repair_agent", "secret.log")
