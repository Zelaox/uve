"""A composite UVE probe. Existing runner owns leases, identity and receipts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from agent_training.content_hash import canonical_json_bytes, content_hash
from agent_training.manifest_loader import load_json_ref, pipeline_root
from agent_training.uve.bundle import resolve_log_dir
from agent_training.uve.evidence_store import EvidenceStore
from agent_training.uve.flow_contract import FlowContractError, MISSING, at, compile_flow, require, typed
from agent_training.uve.flow_result import evaluate
from agent_training.uve.flow_trace import project_events
from agent_training.uve.lease_policy import owns_probe_lease, resources_for_probe


def _owned_path(root: Path, relative: str) -> Path:
    target = root / relative
    resolved = target.resolve()
    require(not Path(relative).is_absolute() and resolved != root and resolved.is_relative_to(root),
            "fixture path escapes the owned directory")
    require(target.absolute() == resolved, "fixture path contains a link or traversal")
    return resolved


def _read_json(path: Path, maximum: int) -> dict:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum, "observation exceeds byte budget")
    value = json.loads(data.decode("utf-8"))
    require(isinstance(value, dict), "observation must be a JSON object")
    return value


def _command(step: dict, root: Path, values: dict, ctx: dict, budget: dict, remaining: float,
             fault: dict | None = None) -> dict:
    spec = load_json_ref(step["command_ref"])
    if spec.get("argument_values_ref"):
        external = load_json_ref(spec["argument_values_ref"])
        require(not (set(values) & external.keys()), "command bindings cannot override runtime identity")
        values = {**external, **values}
    timeout = spec["timeout_seconds"]
    require(type(timeout) in (int, float) and 0 < timeout <= budget["step_seconds"], "invalid command time budget")
    argv = spec["argv"]
    require(isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv), "invalid command argv")
    argv = [arg.format_map(values) for arg in argv]
    require(remaining > 0, "flow execution budget exhausted")
    deadline = time.monotonic() + min(timeout, remaining)
    stdout = _owned_path(root, f"{uuid.uuid4().hex}.stdout")
    stderr = _owned_path(root, f"{uuid.uuid4().hex}.stderr")
    acknowledgement = None
    with stdout.open("xb") as out, stderr.open("xb") as err:
        proc = subprocess.Popen(argv, cwd=ctx["workspace_root"], stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while proc.poll() is None:
                require(time.monotonic() < deadline, "declared command timed out")
                require(os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size <= budget["output_bytes"],
                        "declared command exceeded output budget")
                if fault and os.fstat(out.fileno()).st_size:
                    try:
                        candidate = _read_json(stdout, budget["output_bytes"])
                    except json.JSONDecodeError:
                        candidate = None  # The child may still be writing its acknowledgement.
                    if candidate is not None:
                        require(candidate.get("operation_id") == values["operation_id"], "checkpoint belongs to another operation")
                        require(at(candidate, fault["ack_path"]) is True, "child has not confirmed its checkpoint")
                        require(proc.poll() is None, "child exited before the fault boundary")
                        proc.kill()
                        proc.wait(timeout=budget["process_exit_seconds"])
                        acknowledgement = {**candidate, "injection_confirmed": True}
                        break
                time.sleep(min(budget["poll_seconds"], max(0, deadline - time.monotonic())))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=budget["process_exit_seconds"])
    require(stdout.stat().st_size + stderr.stat().st_size <= budget["output_bytes"], "declared command exceeded output budget")
    epoch = f"process-{proc.pid}-{uuid.uuid4().hex}"
    if fault:
        require(acknowledgement is not None, "child never reached the declared fault boundary")
        return {"state": "passed", "payload": acknowledgement, "epoch": epoch}
    if proc.returncode != spec["expected_returncode"]:
        return {"state": "not_verified", "reason": "command did not produce the required observation", "returncode": proc.returncode}
    payload = _read_json(stdout, budget["output_bytes"])
    require(payload.get("operation_id") == values["operation_id"], "command observation belongs to a different operation")
    return {"state": "passed", "payload": payload, "epoch": epoch}


def _file_read(step: dict, root: Path, values: dict, budget: dict) -> dict:
    payload = _read_json(_owned_path(root, step["fixture_path"]), budget["output_bytes"])
    require(at(payload, step.get("operation_path", ["operation_id"])) == values["operation_id"],
            "file observation belongs to a different operation")
    return {"state": "passed", "payload": payload}


def _file_fault(step: dict, root: Path, values: dict, policy: dict) -> dict:
    fault = policy["fault_actions"].get(step.get("fault_action"))
    require(fault and fault["kind"] == "replace_fixture_file", "fault action is outside the trusted catalog")
    target = _owned_path(root, step["fixture_path"])
    require(target.is_file(), "fault boundary did not produce its fixture file")
    replacement = fault["replacement_utf8"].encode("utf-8")
    with target.open("rb") as stream:
        require(stream.read(len(replacement) + 1) != replacement, "fault had no effect")
    temporary = _owned_path(root, f"{uuid.uuid4().hex}.fault")
    temporary.write_bytes(replacement)
    temporary.replace(target)
    require(target.read_bytes() == replacement, "fault acknowledgement was not observed")
    return {"state": "passed", "payload": {"injection_confirmed": True, "operation_id": values["operation_id"]}}


def _adb(step: dict, ctx: dict, policy: dict, probe_id: str) -> dict:
    require(policy.get("allow_live_adb") is True, "live Android is disabled by the trusted deep-flow policy")
    profile = ctx["profile"]
    require(ctx.get("run_lease_id") and owns_probe_lease(profile, probe_id, ctx["run_lease_id"]), "Android step lacks a held lease")
    adb = load_json_ref(profile["adb_probe_ref"])
    require(("emulator_serial", canonical_json_bytes({"adb_serial": adb["adb_serial"]}).decode("utf-8"))
            in resources_for_probe(profile, probe_id), "Android lease is for another target")
    from agent_training.uve.adapters.adb import run_probe as run_adb

    child_probe = policy["adapters"][step["adapter"]]["probe_id"]
    catalog = ctx["manifest_bundle"]["_loaded"]["config/uve/uve_probe_catalog.json"]
    child = {**ctx, "profile": {**profile, "ui_flow_ref": step["ui_flow_ref"]}, "probe_spec": catalog["probes"][child_probe]}
    result = run_adb(probe_id=child_probe, ctx=child)
    return {"state": result["verdict"], "payload": result, "reason": result.get("failure_class")}


def _remove_fixture(root: Path, fixture_parent: Path) -> bool:
    # rmtree never receives an operator path. Recheck the resolved target before removal.
    if root.resolve().parent != fixture_parent or root.resolve() != root:
        return False
    try:
        shutil.rmtree(root)
    except OSError:
        return False
    return not root.exists()


def _run_case(case: dict, plan: dict, ctx: dict, policy: dict, probe_id: str,
              fixture_parent: Path, deadline: float) -> dict:
    root = Path(tempfile.mkdtemp(dir=fixture_parent)).resolve()
    operation_id = uuid.uuid4().hex
    values = {"python": sys.executable, "workspace_root": str(ctx["workspace_root"]), "fixture_root": str(root),
              "operation_id": operation_id, "run_id": ctx["probe_run_id"], "case_id": case["case_id"]}
    values.update(plan["bindings"])
    budget = policy["budgets"]
    records: dict[str, Any] = {}
    events: list[dict] = []
    raw_event_ids: dict[str, list[str]] = {}
    source_sequences: dict[tuple[str, str], int] = {}
    started = time.monotonic()
    try:
        for step in case["steps"]:
            name = step["step_id"]
            if step["phase"] != "cleanup" and any(records[parent]["state"] != "passed" for parent in step.get("after", [])):
                records[name] = {"state": "not_verified", "reason": "prerequisite step did not pass"}
                continue
            try:
                require(step["phase"] == "cleanup" or time.monotonic() < deadline, "flow execution budget exhausted")
                kind = policy["adapters"][step["adapter"]]["kind"]
                if kind == "command":
                    result = _command(step, root, values, ctx, budget, deadline - time.monotonic())
                elif kind == "fixture_file":
                    result = _file_read(step, root, values, budget)
                elif kind == "fault_file":
                    result = _file_fault(step, root, values, policy)
                elif kind == "fault_process":
                    fault = policy["fault_actions"].get(step.get("fault_action"))
                    require(fault and fault["kind"] == "kill_owned_process", "process fault action is outside the trusted catalog")
                    result = _command(step, root, values, ctx, budget, deadline - time.monotonic(), fault)
                elif kind == "fixture_absent":
                    result = {"state": "passed", "payload": {"absent": not _owned_path(root, step["fixture_path"]).exists()}}
                elif kind == "adb":
                    result = _adb(step, ctx, policy, probe_id)
                else:
                    raise FlowContractError("unimplemented adapter capability")
                if result["state"] != "passed":
                    records[name] = {"state": result["state"], "reason": result.get("reason")}
                    continue
                payload = result["payload"]
                fields = {}
                for field, spec in step.get("fields", {}).items():
                    value = typed(at(payload, spec["path"]), spec)
                    require(value is not MISSING, f"missing or invalid projected field: {field}")
                    fields[field] = value
                trace = step.get("trace")
                if plan["depth"] >= 5 and trace:
                    raw = payload.get("events")
                    if "observe_kind" in trace:
                        epoch = result.get("epoch", ctx["probe_run_id"])
                        source = (trace["source"], epoch)
                        sequence = source_sequences.get(source, 0) + 1
                        parents = [event for parent in trace.get("parents_from", []) for event in raw_event_ids.get(parent, [])]
                        require(not trace.get("parents_from") or parents, "causal parent observation is missing")
                        raw = [{"event_id": uuid.uuid4().hex, "operation_id": operation_id,
                                "kind": trace["observe_kind"], "epoch": epoch, "attempt": epoch,
                                "sequence": sequence, "parents": parents}]
                        source_sequences[source] = sequence
                    projected = project_events(raw, step, operation_id, budget["event_count"] - len(events))
                    events.extend(projected)
                    raw_event_ids[name] = [event["event_id"] for event in raw]
                records[name] = {"state": "passed", "fields": fields}
            except (FlowContractError, OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                reason = str(exc) if isinstance(exc, FlowContractError) else type(exc).__name__
                records[name] = {"state": "not_verified", "reason": reason}
    finally:
        cleaned = _remove_fixture(root, fixture_parent)
    return {"operation_id": operation_id, "steps": records, "events": events, "cleanup_verified": cleaned,
            "duration_ms": round((time.monotonic() - started) * 1000)}


def run_probe(*, probe_id: str, ctx: dict) -> dict:
    policy = ctx["manifest_bundle"]["_loaded"][ctx["probe_spec"]["deep_flow_policy_ref"]]
    depth = ctx["probe_spec"]["depth"]
    try:
        profile = ctx["profile"]
        ref = profile.get("deep_flow_ref")
        require(ref, "profile has no deep flow contract")
        plan = compile_flow(ref, policy, depth)
        require(not ({"python", "workspace_root", "fixture_root", "operation_id", "run_id", "case_id"} & plan["bindings"].keys()),
                "flow bindings shadow runtime arguments")
        require(len(plan["cases"]) <= policy["budgets"]["case_count"], "flow case budget exceeded")
        require(all(len(case["steps"]) <= policy["budgets"]["steps_per_case"] for case in plan["cases"]), "flow step budget exceeded")
        if depth >= 6:
            require(profile.get("deep_flow_scope") in policy["fault_allowed_scopes"], "fault injection requires an isolated target")
            require(ctx["workspace_root"] == pipeline_root().resolve(), "fault commands require the trusted fixture workspace")
            steps = [step for case in plan["cases"] for step in case["steps"]]
            require(all(policy["adapters"][step["adapter"]]["kind"] != "adb" for step in steps),
                    "the owned fixture fault policy cannot target a live Android app")
            approved_refs = {entry["command_ref"] for entry in policy["fault_commands"].values()}
            require(all(step.get("command_ref") in approved_refs for step in steps
                        if policy["adapters"][step["adapter"]]["kind"] in {"command", "fault_process"}),
                    "fault scenario uses a command outside the trusted fixture catalog")
    except (FlowContractError, OSError, KeyError, ValueError, TypeError) as exc:
        return {"verdict": "not_verified", "fault_origin": "engine_fault", "failure_class": "binding_missing",
                "reason": str(exc) if isinstance(exc, FlowContractError) else type(exc).__name__}
    log_root = resolve_log_dir().resolve()
    fixture_parent = Path(load_json_ref(policy["fixture_store_ref"])["work_dir"].format(log_root=log_root)).resolve()
    require(fixture_parent != log_root and fixture_parent.is_relative_to(log_root), "fixture store escapes the pipeline")
    if depth >= 6:
        key = canonical_json_bytes({"work_dir": str(fixture_parent)}).decode("utf-8")
        if (not ctx.get("run_lease_id") or not owns_probe_lease(profile, probe_id, ctx["run_lease_id"])
                or ("mutation_work_dir", key) not in resources_for_probe(profile, probe_id)):
            return {"verdict": "not_verified", "fault_origin": "engine_fault", "failure_class": "binding_missing",
                    "reason": "fault injection lacks a held lease on the owned fixture store"}
    fixture_parent.mkdir(parents=True, exist_ok=True)
    evidence = {}
    deadline = time.monotonic() + policy["budgets"]["flow_seconds"]
    for case in plan["cases"]:
        # Negative controls run only after the baseline cases are demonstrably usable.
        if case["kind"] == "fault":
            baseline = {**plan, "cases": [item for item in plan["cases"] if item["kind"] == "baseline"]}
            if evaluate(baseline, evidence, policy)["verdict"] != "passed":
                break
        evidence[case["case_id"]] = _run_case(case, plan, ctx, policy, probe_id, fixture_parent, deadline)
    result = evaluate(plan, evidence, policy)
    data = canonical_json_bytes({"schema_version": "uve_deep_flow_evidence.v1", "probe_run_id": ctx["probe_run_id"],
                                 "plan_sha256": plan["plan_sha256"], "evidence": evidence})
    require(len(data) <= policy["budgets"]["evidence_bytes"], "projected evidence exceeds its byte budget")
    artifact = EvidenceStore().write_artifact(ctx["role_id"], data, media_type="application/json")
    verdict = result["verdict"]
    return {**result, "flow_id": plan["flow_id"], "depth": depth, "deep_flow_plan_sha256": plan["plan_sha256"],
            "flow_evidence": artifact, "artifacts": [artifact],
            "fault_origin": "none" if verdict == "passed" else "subject_fault" if verdict == "failed" else "engine_fault",
            "failure_class": None if verdict == "passed" else "assertion_mismatch" if verdict == "failed" else "flow_evidence_missing"}
