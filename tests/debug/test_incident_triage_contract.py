import json

from mcp_server.diagnostics.incident_triage import triage_turn


def test_triage_is_read_only_and_classifies_dispatch_contradiction(tmp_path):
    turn = "turn-1"
    event_dir = tmp_path / "data/runtime/turn_events"
    event_dir.mkdir(parents=True)
    rows = [
        {"timestamp": "1", "stage": "cip_intent_classification", "data": {"intent": "image_generation"}},
        {"timestamp": "2", "stage": "cip_work_mode_override", "data": {"intent": "code_assist"}},
    ]
    path = event_dir / f"{turn}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    before = path.read_bytes()
    result = triage_turn(turn, root=tmp_path)

    assert result["mode"] == "read_only"
    assert "DISPATCH_INTENT_CONTRADICTION" in {f["code"] for f in result["findings"]}
    assert path.read_bytes() == before
