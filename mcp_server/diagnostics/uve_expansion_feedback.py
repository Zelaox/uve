"""Run a manifest-bound expansion scenario and collect its independent UVE evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp

from .engine_uve_validate import _load_json, _resolve_binary, _resolve_scenario_ids, _run, resolve_engine_root
from .uve_test_expansion_evolution import _pipeline_root
from .uve_triage_proof import validate_gate
from .uve_triage_sources import pipeline_document
from .uve_triage_store import json_text, load_config, sha256, stable_id, within


def load_feedback(lab_root, binding):
    return _load_json(_pipeline_root(lab_root, binding) / binding["expansion_feedback_policy_relative_in_pipeline"])


def scenario_binding(lab_root, binding, policy, proposal):
    engine = resolve_engine_root(lab_root, binding)
    matches = [row for row in policy["scenario_bindings"]
               if row["parent_scenario"] == proposal["parent_scenario"]
               and row["strategy_id"] == proposal["strategy_id"]
               and row["coverage"] == proposal["missing_coverage"]]
    if len(matches) != 1:
        raise ValueError("Proposal does not have exactly one registered evaluation scenario")
    scenario = matches[0]["scenario_id"]
    contract = _load_json(engine / binding["verification_manifest_relative_from_engine"])["engine_contracts"]
    if scenario not in _resolve_scenario_ids(engine, contract):
        raise ValueError("Evaluation scenario is outside the engine verification manifest")
    guards = _load_json(engine / policy["critical_guard_requirements_relative_from_engine"])
    requirements = sorted(key for key in guards["requirement_ids"] if scenario in guards["scenario_refs"][key])
    runner = _resolve_binary(env_key=contract["test_runner_env_key"],
                             relative_candidates=contract["test_runner_candidates_relative_from_lab"], base=lab_root)
    return scenario, requirements, runner, contract["verify_seed"]


def run_scenario(policy, request, lab_root):
    """Command probe body. Confirm the selected binary before and after execution."""
    runner = Path(request["runner"])
    if sha256(runner.read_bytes()) != request["runner_sha256"]:
        raise ValueError("Scenario runner differs from the bound binary")
    result = _run([str(runner), "--verify-scenario", request["scenario_id"], "--seed", str(request["seed"])],
                  cwd=lab_root, timeout=policy["timeout_seconds"])
    if sha256(runner.read_bytes()) != request["runner_sha256"]:
        raise ValueError("Scenario runner changed during execution")
    print(result.stdout or "", end="")
    marker = f"{policy['scenario_success_token']} {request['scenario_id']}"
    if result.returncode or not any(line.split()[:2] == marker.split() for line in (result.stdout or "").splitlines()):
        print(result.stderr or "", file=sys.stderr)
        return 1
    print("\n" + policy["output_marker"] + " " + request["inputs_hash"])
    return 0


def evaluate_proposal(lab_root, binding, proposal, *, actor):
    config = load_config(lab_root)
    policy = load_feedback(lab_root, binding)
    scenario, requirements, runner, seed = scenario_binding(lab_root, binding, policy, proposal)
    log_policy = pipeline_document(config, config.policy("ingest_ref")["uve_log_root_ref_in_pipeline"])
    log_root = within(config.pipeline, log_policy["log_dir"])
    work_root = within(log_root, policy["run_directory_relative"])
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(dir=work_root))
    inputs = {
        "proposal_id": proposal["proposal_id"], "scenario_id": scenario, "requirement_ids": requirements,
        "runner": str(runner), "runner_sha256": sha256(runner.read_bytes()), "seed": seed,
        "policy_sha256": stable_id(policy),
    }
    inputs["inputs_hash"] = stable_id({key: value for key, value in inputs.items() if key != "runner"})
    request = run_dir / "request.json"
    command = run_dir / "command.json"
    profile = run_dir / "profile.json"
    request.write_text(json_text(inputs), encoding="utf-8")
    marker = f"{policy['output_marker']} {inputs['inputs_hash']}"
    command.write_text(json_text({
        "schema_version": "uve_command_probe.v1",
        "argv": ["{python}", "-B", policy["lifecycle_script_relative"], "--run-scenario", str(request)],
        "request_ref": str(request), "timeout_seconds": policy["timeout_seconds"],
        "expected_returncode": 0, "expected_stdout": marker,
    }), encoding="utf-8")
    base = pipeline_document(config, binding["uve_profile_relative_in_pipeline"])
    # Watch precisely the code and manifests that bind the native runner to this proposal.
    from scripts.uve.verify_observability import unobserved_directories

    base["identity"]["watch_globs"].extend(policy["watch_globs"])
    engine = resolve_engine_root(lab_root, binding)
    derived = [engine / binding[key] for key in (
        "expansion_strategy_rank_overlay_relative_from_engine", "expansion_proposal_index_relative_from_engine",
    )]
    base["identity"]["ignore_globs"].extend(
        path.relative_to(lab_root).as_posix() for path in derived if path.is_relative_to(lab_root)
    )
    base["identity"]["watch_globs"].append(
        (_pipeline_root(lab_root, binding) / binding["expansion_feedback_policy_relative_in_pipeline"])
        .relative_to(lab_root).as_posix()
    )
    base["identity"]["ignore_globs"].extend(unobserved_directories(lab_root, base["identity"]["watch_globs"]))
    probe_id = policy["probe_id"]
    base["command_probe_refs"] = {probe_id: str(command)}
    base["verification_probe_ids"] = [probe_id]
    base["gate_ids"] = [policy["gate_id"]]
    profile.write_text(json_text(base), encoding="utf-8")
    env = {**os.environ, config.policy("lab_binding_ref")["lab_root_env_key"]: str(lab_root), "PYTHONUTF8": "1"}

    def invoke(name, *args):
        process = subprocess.run([sys.executable, "-B", "-m", "agent_training.uve.cli", *args],
                                 cwd=config.pipeline, env=env, encoding="utf-8", capture_output=True,
                                 timeout=policy["timeout_seconds"], check=False)
        (run_dir / name).write_text(process.stdout, encoding="utf-8")
        if not process.stdout:
            raise ValueError(f"UVE produced no receipt for {name}")
        return json.loads(process.stdout)

    probe = invoke("probe.json", "probe", "run", "--profile", str(profile), "--probe", probe_id,
                   "--repair-actor-ref", actor, "--index")
    gate = invoke("gate.json", "gate", "evaluate", "--profile", str(profile), "--gate", policy["gate_id"],
                  "--probe-run-id", probe["probe_run_id"], "--repair-actor-ref", actor,
                  "--evaluator-ref", binding["default_gate_evaluator_ref"], "--index")
    if gate["decision"] != "approved":
        raise ValueError(f"Evaluation {gate['decision']}. See {run_dir / 'gate.json'}")
    gate_path = within(log_root, Path("gates") / (gate["trust_chain_ref"] + ".json"))
    approved = validate_gate(config, gate_path)
    ref = next(ref for ref in approved["probe_receipt_refs"] if ref["probe_run_id"] == probe["probe_run_id"])
    if marker not in probe["observation"].get("stdout", ""):
        raise ValueError("Approved probe omitted the proposal evaluation marker")
    return {
        **{key: value for key, value in inputs.items() if key != "runner"},
        "evaluation_id": stable_id(inputs["inputs_hash"], probe["subject_identity"]["manifest_bundle_sha256"]),
        "subject_identity_sha256": probe["subject_identity"]["identity_sha256"],
        "proposal_outcome": policy["positive_outcomes"]["critical_guard" if requirements else "scenario_pass"],
        "probe_run_id": probe["probe_run_id"], "probe_sha256": ref["receipt_sha256"],
        "gate_ref": gate["trust_chain_ref"], "gate_sha256": sha256(gate_path.read_bytes()),
    }
