#!/usr/bin/env python3
"""Proposal lifecycle: accept / reject / implement / verify (advisory — no manifest merge)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.engine_uve_validate import load_engine_binding
from mcp_server.diagnostics.uve_expansion_feedback import load_feedback, run_scenario
from mcp_server.diagnostics.uve_test_expansion_advisor import transition_proposal_status
from mcp_server.diagnostics.uve_test_expansion_evolution import run_evolution_generation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true")
    group.add_argument("--reject", action="store_true")
    group.add_argument("--implement", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--run-scenario", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--outcome")
    parser.add_argument("--actor")
    parser.add_argument("--evolve", action="store_true", help="Publish the next ranking after verified evaluation")
    args = parser.parse_args()
    policy = load_feedback(ROOT, load_engine_binding(ROOT))
    if args.run_scenario:
        return run_scenario(policy, json.loads(args.run_scenario.read_text(encoding="utf-8")), ROOT)
    if not args.proposal_id:
        parser.error("--proposal-id is required")
    if args.verify and (not args.actor or args.outcome):
        parser.error("--verify requires --actor and derives its outcome from UVE")
    if args.evolve and not args.verify:
        parser.error("--evolve requires --verify")

    if args.accept:
        status = "accepted"
        outcome = None
    elif args.reject:
        status = "rejected"
        outcome = args.outcome or policy["default_rejection_outcome"]
    elif args.implement:
        status = "implemented"
        outcome = None
    else:
        status = "verified"
        outcome = None

    record = transition_proposal_status(
        ROOT,
        args.proposal_id,
        status,
        proposal_outcome=outcome,
        actor=args.actor,
    )
    result = {"ok": True, "proposal": record}
    if args.evolve:
        result["evolution"] = run_evolution_generation(ROOT)
        from mcp_server.diagnostics.uve_triage_adapter import index_sidecar

        result["observability_index"] = index_sidecar(ROOT, "advisor_mirror")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
