"""Real composite probe and negative contract controls on an isolated target."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_training.uve import bundle, evidence_store, flow_replay
from agent_training.uve.flow_contract import FlowContractError, compile_flow
from agent_training.uve.flow_trace import check_edges, project_events
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe


def _json(path: Path, doc: dict) -> str:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _edge() -> dict:
    return {"edge_id": "handoff", "from": {"step": "write", "kind": "saved"},
            "to": {"step": "read", "kind": "loaded"}, "different_sources": True, "relation": "new_epoch"}


def _trace() -> list[dict]:
    op = "isolated-operation"
    raw = {"operation_id": op, "attempt": "try-a", "sequence": 1, "epoch": "process-a", "parents": []}
    first = project_events([{**raw, "event_id": "first", "kind": "saved"}],
                           {"step_id": "write", "trace": {"source": "writer", "kinds": ["saved"]}}, op, 8)
    last = project_events([{**raw, "event_id": "last", "kind": "loaded", "epoch": "process-b", "parents": ["first"]}],
                          {"step_id": "read", "trace": {"source": "reader", "kinds": ["loaded"]}}, op, 8)
    return first + last


def test_trace_uses_causality_accepts_out_of_order_and_exact_duplicates():
    first, last = _trace()
    assert check_edges([last, first, last], [_edge()])[0]["state"] == "passed"
    assert check_edges([first], [_edge()])[0]["state"] == "not_verified"
    with pytest.raises(FlowContractError, match="absent cause"):
        check_edges([last], [_edge()])
    with pytest.raises(FlowContractError, match="conflicting duplicate"):
        check_edges([first, last, {**first, "sequence": 2}], [_edge()])
    with pytest.raises(FlowContractError, match="cycle"):
        check_edges([{**first, "parents": [last["event_id"]]}, last], [_edge()])
    with pytest.raises(FlowContractError, match="sequence gap"):
        check_edges([{**first, "sequence": 2}, last], [_edge()])


@pytest.fixture
def isolated_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "resolve_log_dir", lambda: tmp_path / "logs")
    # Imported aliases in the runner and the adapter share the same isolated store.
    import agent_training.uve.orchestrator as runner
    import agent_training.uve.adapters.deep_flow as adapter
    import agent_training.uve.flow_status as status
    monkeypatch.setattr(runner, "resolve_log_dir", bundle.resolve_log_dir)
    monkeypatch.setattr(adapter, "resolve_log_dir", bundle.resolve_log_dir)
    monkeypatch.setattr(status, "resolve_log_dir", bundle.resolve_log_dir)
    monkeypatch.setattr(evidence_store, "pipeline_root", lambda: tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    driver = workspace / "probe.py"
    driver.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "data={'operation_id':sys.argv[2],'revision':int(sys.argv[3])}\n"
        "Path(sys.argv[1]).write_text(json.dumps(data),encoding='utf-8')\n"
        "print(json.dumps({'operation_id':sys.argv[2],'saved':True}))\n", encoding="utf-8")
    monkeypatch.setenv("UVE_TEST_FLOW_ROOT", str(workspace))
    root_ref = _json(tmp_path / "root.json", {"root_env": "UVE_TEST_FLOW_ROOT"})
    binding_ref = _json(tmp_path / "bindings.json", {"revision": 4, "expected": 4})
    command_ref = _json(tmp_path / "command.json", {
        "argv": ["{python}", "probe.py", "{fixture_root}/data.json", "{operation_id}", "{revision}"],
        "timeout_seconds": 10, "expected_returncode": 0})
    flow = {
        "schema_version": "uve_deep_flow.v1", "flow_id": "test_persistence",
        "bindings": {name: {"type": "integer", "value_ref": binding_ref, "path": [name]} for name in ("revision", "expected")},
        "cases": [{"case_id": "save", "kind": "baseline", "steps": [
            {"step_id": "write", "phase": "setup", "adapter": "command", "command_ref": command_ref,
             "fields": {"saved": {"type": "boolean", "path": ["saved"]}},
             "trace": {"source": "writer", "kinds": ["saved"], "observe_kind": "saved"}},
            {"step_id": "read", "phase": "observe", "adapter": "fixture_file", "fixture_path": "data.json", "after": ["write"],
             "fields": {"revision": {"type": "integer", "path": ["revision"]}},
             "trace": {"source": "reader", "kinds": ["loaded"], "observe_kind": "loaded", "parents_from": ["write"]}}
        ], "invariants": [{"invariant_id": "revision_stays", "operator": "eq", "severity": "major",
                            "refs": [{"step": "read", "field": "revision"}, {"binding": "expected"}]}],
            "trace_edges": [_edge()]}]}
    profile = _json(tmp_path / "profile.json", {
        "schema_version": "uve_project_profile.v1", "project_id": "fixture", "environment": "isolated",
        "project_root_ref": root_ref, "deep_flow_ref": _json(tmp_path / "flow.json", flow),
        "identity": {"watch_globs": ["probe.py"], "ignore_globs": []}})
    return profile, Path(binding_ref), flow, tmp_path


def test_real_probe_replay_binding_and_gate_reject_tampering(isolated_probe):
    profile, binding, _, temp = isolated_probe
    receipt = run_probe(profile, "deep_flow_v5", repair_actor_refs=["fixture-author"])
    assert receipt["verdict"] == "passed", receipt["observation"]
    assert flow_replay.replay(receipt, "uve_runner")["matches_receipt"] is True
    store = evidence_store.EvidenceStore()
    with pytest.raises(Exception, match="lacks capability"):
        store.read_artifact("repair_agent", receipt["artifacts"][0], maximum_bytes=100000)
    bundle_hash = bundle.load_manifest_bundle()["manifest_bundle_sha256"]
    assert evaluate_gate("deep_flow_v5", [receipt], manifest_bundle_sha256=bundle_hash,
                         gate_evaluator_ref="fixture-author")["decision"] == "not_verified"
    tampered = deepcopy(receipt)
    tampered["observation"]["cases"][0]["invariants"][0]["operands"] = [0, 0]
    gate = evaluate_gate("deep_flow_v5", [tampered], manifest_bundle_sha256=bundle_hash,
                         gate_evaluator_ref="test-contract-evaluator")
    assert gate["decision"] == "rejected" and gate["rejection_basis"] == "invalid_stale"
    _json(binding, {"revision": 4, "expected": 5})
    with pytest.raises(FlowContractError, match="configuration has changed"):
        flow_replay.replay(receipt, "uve_runner")
    mismatch = run_probe(profile, "deep_flow_v5")
    assert mismatch["verdict"] == "failed"
    assert flow_replay.triage(mismatch)["primary"]["subject"] == "revision_stays"
    _json(binding, {"revision": 5, "expected": 5})
    assert run_probe(profile, "deep_flow_v5")["verdict"] == "passed"
    assert not list((temp / "logs" / "flow-fixtures").iterdir())


def test_incomplete_bindings_and_unsafe_fault_scope_do_not_execute(isolated_probe):
    profile, binding, _, temp = isolated_probe
    _json(binding, {"revision": 4})
    result = run_probe(profile, "deep_flow_v5")
    assert result["verdict"] == "not_verified" and result["failure_class"] == "binding_missing"
    policy = bundle.load_manifest_bundle()["_loaded"]["config/uve/uve_deep_flow_manifest.json"]
    with pytest.raises(FlowContractError, match="needs a fault scenario"):
        compile_flow("config/uve/fixtures/deep_protrainer_comments/flow.json", policy, 6)
    external = json.loads(Path(profile).read_text(encoding="utf-8"))
    external.update(deep_flow_ref="config/uve/fixtures/deep_workstation/flow.json", deep_flow_scope="production")
    rejected = run_probe(_json(temp / "production.json", external), "deep_flow_v6")
    assert rejected["verdict"] == "not_verified"
    assert "requires an isolated target" in rejected["observation"]["reason"]


def test_command_encoding_failure_is_an_adapter_fault(tmp_path):
    from agent_training.uve.adapters.command import run_probe as command

    ref = _json(tmp_path / "command.json", {"argv": ["{python}", "-c", "import sys; sys.stdout.buffer.write(bytes([255]))"],
                                            "timeout_seconds": 10, "expected_returncode": 0})
    result = command(probe_id="command_check", ctx={"profile": {"command_probe_refs": {"command_check": ref}},
                                                    "workspace_root": tmp_path})
    assert result["verdict"] == "not_verified" and result["failure_class"] == "adapter_error"
