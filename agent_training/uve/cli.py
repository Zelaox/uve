from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_training.manifest_loader import load_json_ref, pipeline_root
from agent_training.uve.bundle import resolve_log_dir, write_json_atomic
from agent_training.uve.export_training import apply_secret_placeholders, build_export_row
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import load_profile, run_probe


def _load_probe_receipt(probe_run_id: str) -> dict:
    root = (resolve_log_dir() / "probes").resolve()
    path = (root / f"{probe_run_id}.json").resolve()
    if path.parent != root:
        raise ValueError("probe id must be a single artifact name")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_probe_run(args: argparse.Namespace) -> int:
    receipt = run_probe(args.profile, args.probe, dry_mode=args.dry_mode, repair_actor_refs=args.repair_actor_ref)
    print(json.dumps(receipt, indent=2))
    if receipt["verdict"] == "passed":
        return 0
    if receipt["verdict"] == "failed":
        return 1
    return 2


def cmd_plan(args: argparse.Namespace) -> int:
    from agent_training.uve.planner import plan_probes_for_changes
    from agent_training.uve.workspace import resolve_workspace_root

    profile = load_profile(args.profile)
    root = resolve_workspace_root(profile)
    previous = json.loads(Path(args.previous_inventory).read_text(encoding="utf-8")) if args.previous_inventory else None
    planned = plan_probes_for_changes(profile, root, previous)
    if args.write_inventory:
        from agent_training.uve.identity import build_inventory_records
        Path(args.write_inventory).write_text(json.dumps(build_inventory_records(root, profile), indent=2), encoding="utf-8")
    print(json.dumps({"probe_ids": planned}, indent=2))
    return 0


def cmd_recovery(args: argparse.Namespace) -> int:
    from agent_training.uve.recovery import run_recovery_drill

    out = run_recovery_drill(args.profile, args.action_id)
    print(json.dumps(out, indent=2))
    return 0 if out["status"] == "complete" else 2


def cmd_flow_inspect(args: argparse.Namespace) -> int:
    from agent_training.uve.flow_replay import compare, replay, triage

    receipt = _load_probe_receipt(args.probe_run_id)
    if args.flow_action == "replay":
        result = replay(receipt, "uve_runner")
    elif args.flow_action == "compare":
        result = compare(receipt, _load_probe_receipt(args.with_probe_run_id), "uve_runner")
    else:
        result = triage(receipt, args.registry_ref)
    print(json.dumps(result, indent=2))
    return 0 if result.get("matches_receipt", True) else 2


def cmd_flow_status(_args: argparse.Namespace) -> int:
    from agent_training.uve.bundle import load_manifest_bundle
    from agent_training.uve.flow_status import collect_deep_status

    bundle = load_manifest_bundle()
    print(json.dumps(collect_deep_status(bundle["manifest_bundle_sha256"]), indent=2))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    from agent_training.uve.status import collect_uve_status

    print(json.dumps(collect_uve_status(), indent=2))
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    from agent_training.uve.diagnostics import diagnose_runtime

    status_path = args.status_path
    if status_path is None:
        runtime = load_json_ref(args.runtime_manifest)
        status_path = resolve_log_dir() / runtime["status_relative"]
    result = diagnose_runtime(status_path, args.context)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["evidence_consistent"] and result["context_matches_status"] is not False else 2


def cmd_gate_evaluate(args: argparse.Namespace) -> int:
    from agent_training.uve.bundle import load_manifest_bundle

    bundle = load_manifest_bundle()
    probes = [_load_probe_receipt(pid) for pid in args.probe_run_id]
    gate = evaluate_gate(
        args.gate,
        probes,
        manifest_bundle_sha256=bundle["manifest_bundle_sha256"],
        gate_evaluator_ref=args.evaluator_ref,
        repair_actor_refs=args.repair_actor_ref,
    )
    write_json_atomic(resolve_log_dir() / "gates" / f"{gate['trust_chain_ref']}.json", gate)
    if any(probe["probe"]["adapter_id"] == "deep_flow" for probe in probes):
        from agent_training.uve.flow_status import index_gate

        index_gate(gate, probes)
    if gate["decision"] in ("rejected", "not_verified"):
        profile_ref = args.profile if hasattr(args, "profile") and args.profile else None
        if profile_ref:
            prof = load_profile(profile_ref)
            from agent_training.uve.issue_bridge import sync_gate_to_issue

            sync_gate_to_issue(gate, prof["project_id"])
    print(json.dumps(gate, indent=2))
    if gate["decision"] == "approved":
        return 0
    if gate["decision"] == "rejected":
        return 1
    return 2


def cmd_export_row(args: argparse.Namespace) -> int:
    gate_path = Path(args.gate_receipt)
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    probes = [_load_probe_receipt(r["probe_run_id"]) for r in gate["probe_receipt_refs"]]
    profile = load_profile(args.profile)
    gate_spec = load_json_ref("config/uve/uve_gate_manifest.json")["gates"][args.gate]
    row = build_export_row(
        failure_class=args.failure_class,
        gate_receipt=gate,
        probe_receipts=probes,
        required_probe_ids=gate_spec["required_probe_ids"],
        fields=json.loads(args.fields_json) if args.fields_json else {},
    )
    if args.raw_log_excerpt:
        row["log_excerpt"] = apply_secret_placeholders(args.raw_log_excerpt, args.placeholder_ref)
    print(json.dumps(row, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uve")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe")
    p_probe_sub = p_probe.add_subparsers(dest="probe_cmd", required=True)
    pr = p_probe_sub.add_parser("run")
    pr.add_argument("--profile", required=True)
    pr.add_argument("--probe", required=True)
    pr.add_argument("--dry-mode", default=None)
    pr.add_argument("--repair-actor-ref", action="append", default=[])
    pr.set_defaults(func=cmd_probe_run)

    p_gate = sub.add_parser("gate")
    p_flow = sub.add_parser("flow", help="Inspect projected flow evidence without touching its target")
    flow_commands = p_flow.add_subparsers(dest="flow_action", required=True)
    flow_commands.add_parser("status").set_defaults(func=cmd_flow_status)
    for action in ("replay", "triage", "compare"):
        command = flow_commands.add_parser(action)
        command.add_argument("--probe-run-id", required=True)
        if action == "triage":
            command.add_argument("--registry-ref")
        if action == "compare":
            command.add_argument("--with-probe-run-id", required=True)
        command.set_defaults(func=cmd_flow_inspect)
    p_gate_sub = p_gate.add_subparsers(dest="gate_cmd", required=True)
    ge = p_gate_sub.add_parser("evaluate")
    ge.add_argument("--gate", required=True)
    ge.add_argument("--probe-run-id", action="append", required=True, dest="probe_run_id")
    ge.add_argument("--evaluator-ref", default="uve_gate_cli")
    ge.add_argument("--repair-actor-ref", action="append", default=[])
    ge.add_argument("--profile", default=None, help="optional profile for issue bridge on gate failure")
    ge.set_defaults(func=cmd_gate_evaluate)

    ex = sub.add_parser("export-row")
    ex.add_argument("--profile", required=True)
    ex.add_argument("--gate", required=True)
    ex.add_argument("--gate-receipt", required=True)
    ex.add_argument("--failure-class", required=True)
    ex.add_argument("--fields-json", default="")
    ex.add_argument("--raw-log-excerpt", default="")
    ex.add_argument("--placeholder-ref", default="config/uve/uve_secret_placeholder_manifest.json")
    ex.set_defaults(func=cmd_export_row)

    wit = sub.add_parser("witness")
    wit_sub = wit.add_subparsers(dest="witness_cmd", required=True)
    wr = wit_sub.add_parser("run")
    wr.add_argument("--candidate-root", type=Path)
    wr.set_defaults(func=lambda a: _cmd_witness_run(a.candidate_root))

    reg = sub.add_parser("regression")
    reg_sub = reg.add_subparsers(dest="reg_cmd", required=True)
    rc = reg_sub.add_parser("capture")
    rc.add_argument("--baseline-id", required=True)
    rc.set_defaults(func=cmd_regression_capture)
    rp = reg_sub.add_parser("compare")
    rp.add_argument("--baseline-id", required=True)
    rp.set_defaults(func=cmd_regression_compare)

    pl = sub.add_parser("plan")
    pl.add_argument("--profile", required=True)
    pl.add_argument("--previous-inventory")
    pl.add_argument("--write-inventory")
    pl.set_defaults(func=cmd_plan)

    rec = sub.add_parser("recovery")
    rec.add_argument("--profile", required=True)
    rec.add_argument("--action", required=True, dest="action_id")
    rec.set_defaults(func=cmd_recovery)

    st = sub.add_parser("status")
    st.set_defaults(func=cmd_status)

    dg = sub.add_parser("diagnose", help="Explain stored runtime receipts without running ADB or changing gates")
    dg.add_argument("--runtime-manifest", default="config/uve/uve_workstation_runtime.json")
    dg.add_argument("--status-path", type=Path)
    dg.add_argument("--context", type=Path)
    dg.set_defaults(func=cmd_diagnose)

    serve = sub.add_parser("serve", help="Run emulator-owned verification without starting AI Lab")
    serve.add_argument("--runtime-manifest", required=True)
    serve.add_argument("--context", type=Path, required=True)
    serve.add_argument("--parent-pid", type=int, required=True)
    serve.add_argument("--stop-path", type=Path, required=True)
    serve.add_argument("--status-path", type=Path, required=True)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


def cmd_serve(args: argparse.Namespace) -> int:
    from agent_training.uve.runtime import serve_runtime

    result = serve_runtime(args.runtime_manifest, args.context, args.parent_pid, args.stop_path, args.status_path)
    return 0 if result["state"] == "stopped" else 2


def _cmd_witness_run(candidate_root: Path | None = None) -> int:
    from agent_training.uve.witness import run_witness_bundle

    result = run_witness_bundle(candidate_root)
    print(json.dumps(result, indent=2))
    return 0 if result["approved"] else 1


def cmd_regression_capture(args: argparse.Namespace) -> int:
    from agent_training.uve.regression import capture_baseline

    print(json.dumps(capture_baseline(args.baseline_id), indent=2))
    return 0


def cmd_regression_compare(args: argparse.Namespace) -> int:
    from agent_training.uve.regression import compare_baseline

    out = compare_baseline(args.baseline_id)
    print(json.dumps(out, indent=2))
    return 1 if out["regressed"] else 0


if __name__ == "__main__":
    sys.exit(main())
