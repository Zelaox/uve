from __future__ import annotations

import os
from pathlib import Path

from agent_training.paths import pipeline_root
from agent_training.uve.adapters.ai_lab_bridge import run_probe
from agent_training.uve.ai_lab_map import tier_json_to_probe_receipt


def test_tier_json_maps_to_probe_receipt():
    fixture = pipeline_root() / "config/uve/fixtures/tier_v1_minimal.json"
    doc = __import__("json").loads(fixture.read_text(encoding="utf-8"))
    mapped = tier_json_to_probe_receipt(doc)
    assert mapped["verdict"] == "passed"
    assert mapped["probe"]["tier"] == "V1"


def test_ai_lab_bridge_with_tier_env(monkeypatch):
    fixture = pipeline_root() / "config/uve/fixtures/tier_v1_minimal.json"
    monkeypatch.setenv("UVE_AI_LAB_TIER_JSON", str(fixture))
    out = run_probe(probe_id="ai_lab_tier", ctx={"profile": {}, "probe_spec": {}})
    assert out["verdict"] == "passed"
