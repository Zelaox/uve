from __future__ import annotations

import json

import pytest

from agent_training.content_hash import content_hash
from agent_training.uve import diagnostics
from agent_training.uve.cli import main


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "resolve_log_dir", lambda: tmp_path)
    (tmp_path / "probes").mkdir()
    (tmp_path / "gates").mkdir()
    context = {"adb_serial": "", "device_id": "selected-avd", "owner_pid": 7}
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    probe = {"probe_run_id": "run-1", "probe": {"probe_id": "workstation_transport"},
             "subject_identity": {"identity_sha256": "subject"}, "verdict": "not_verified",
             "fault_origin": "unknown", "observation": {"reason": "missing runtime bindings",
                                                          "missing_fields": ["adb_serial"]}}
    probe_path = tmp_path / "probes" / "run-1.json"
    probe_path.write_text(json.dumps(probe), encoding="utf-8")
    gate = {"trust_chain_ref": "gate-1", "decision": "not_verified",
            "rejection_basis": "insufficient_probes", "subject_identity_sha256": "subject",
            "probe_receipt_refs": [{"probe_run_id": "run-1", "receipt_sha256": content_hash(probe)}]}
    (tmp_path / "gates" / "gate-1.json").write_text(json.dumps(gate), encoding="utf-8")
    status = {"state": "running", "context_sha256": content_hash(context),
              "tiers": [{"tier_id": "V2", "gate": gate}]}
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    return status_path, context_path, probe_path


def test_diagnose_explains_missing_binding_without_changing_evidence(evidence):
    status, context, probe = evidence
    before = {p: p.read_bytes() for p in status.parent.rglob("*.json")}
    report = diagnostics.diagnose_runtime(status, context)
    assert report["evidence_consistent"]
    assert report["context_matches_status"]
    assert report["selected_target"]["adb_serial"] == ""
    assert report["tiers"][0]["probes"][0]["observation"]["missing_fields"] == ["adb_serial"]
    assert before == {p: p.read_bytes() for p in status.parent.rglob("*.json")}


def test_diagnose_detects_stale_context_and_cli_reports_it(evidence, capsys):
    status, context, _ = evidence
    context.write_text('{"adb_serial":"new-selection"}', encoding="utf-8")
    assert main(["diagnose", "--status-path", str(status), "--context", str(context)]) == 2
    assert json.loads(capsys.readouterr().out)["context_matches_status"] is False


@pytest.mark.parametrize("change", ["tamper", "delete", "traversal", "gate"])
def test_diagnose_detects_broken_receipt_chain(evidence, change):
    status, context, probe = evidence
    if change == "tamper":
        value = json.loads(probe.read_text())
        value["verdict"] = "passed"
        probe.write_text(json.dumps(value))
    elif change == "delete":
        probe.unlink()
    elif change == "gate":
        path = status.parent / "gates" / "gate-1.json"
        value = json.loads(path.read_text())
        value["decision"] = "approved"
        path.write_text(json.dumps(value))
    else:
        value = json.loads(status.read_text())
        value["tiers"][0]["gate"]["trust_chain_ref"] = "../../outside"
        status.write_text(json.dumps(value))
    report = diagnostics.diagnose_runtime(status, context)
    assert report["evidence_consistent"] is False
    assert report["evidence_errors"]
