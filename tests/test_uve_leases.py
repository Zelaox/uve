from __future__ import annotations

from agent_training.uve.lease_policy import acquire_probe_lease, release_probe_lease
from agent_training.uve.leases import LeaseManager
from agent_training.manifest_loader import load_json_ref


def test_parallel_lease_blocks_second_holder():
    profile = load_json_ref("config/uve/profiles/protrainer_development.json")
    first, fail1 = acquire_probe_lease(profile, "metro_status", "holder_a")
    assert first is not None
    assert fail1 is None
    second, fail2 = acquire_probe_lease(profile, "metro_status", "holder_b")
    assert second is None
    assert fail2 is not None
    assert fail2["failure_class"] == "lease_denied"
    release_probe_lease(profile, "metro_status", first)
    third, fail3 = acquire_probe_lease(profile, "metro_status", "holder_b")
    assert third is not None
    release_probe_lease(profile, "metro_status", third)


def test_orchestrator_lease_denied_receipt(monkeypatch):
    import os

    from agent_training.paths import pipeline_root
    from agent_training.uve.orchestrator import run_probe

    monkeypatch.setenv("UVE_PROTRAINER_ROOT", str(pipeline_root()))
    profile_ref = "config/uve/profiles/protrainer_development.json"
    profile = load_json_ref(profile_ref)
    lm = LeaseManager()
    port = load_json_ref("config/uve/targets/protrainer/metro_port.json")
    from agent_training.uve.lease_policy import resources_for_probe
    _, key = resources_for_probe(profile, "metro_status")[0]
    lease_id = lm.acquire("dev_server_port", key, "blocker")
    assert lease_id
    receipt = run_probe(profile_ref, "metro_status")
    assert receipt["verdict"] == "not_verified"
    assert receipt["failure_class"] == "lease_denied"
    lm.release("dev_server_port", key, lease_id)
