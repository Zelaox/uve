#!/usr/bin/env python3
"""Emit a Lab ↔ UVE incident bundle (source-read-only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.lab_uve_incident_bundle import (  # noqa: E402
    build_incident_bundle,
    write_incident_bundle,
)


def _resolve_run(run_arg: str) -> Path:
    p = Path(run_arg)
    if p.is_file():
        return p.resolve()
    for candidate in (
        ROOT / run_arg,
        ROOT / "data" / "work_runs" / run_arg,
        ROOT / "data" / "work_runs" / f"{run_arg}.json",
    ):
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(run_arg)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turn-id", default="")
    parser.add_argument("--run", default="")
    parser.add_argument("--probe-run-id", default="")
    parser.add_argument("--write-bundle", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--json", dest="print_json", action="store_true")
    args = parser.parse_args()
    run_path = _resolve_run(args.run) if args.run else None
    if not args.turn_id and not run_path and not args.probe_run_id:
        parser.error("provide --turn-id, --run, and/or --probe-run-id")
    bundle = build_incident_bundle(
        repo_root=ROOT,
        turn_id=args.turn_id,
        run_path=run_path,
        probe_run_id=args.probe_run_id,
    )
    if args.write_bundle:
        paths = write_incident_bundle(repo_root=ROOT, bundle=bundle, output_dir=args.output_dir)
        bundle = {**bundle, "written_paths": {k: str(v) for k, v in paths.items()}}
    if args.print_json or args.write_bundle:
        print(json.dumps(bundle, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
