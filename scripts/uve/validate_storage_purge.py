"""Independent command probe for a current, fully bound purge plan."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
from mcp_server.diagnostics.uve_triage_storage_purge import plan
from mcp_server.diagnostics.uve_triage_store import load_config, open_store, within


def main():
    config = load_config(ROOT)
    policy, digest = policies(config)
    verification = policy['verification']
    try:
        directory = config.path(verification['request_output_relative'])
        path = Path(os.environ[verification['request_env']])
        request = json.loads(within(directory, path.relative_to(directory)).read_text(encoding='utf-8'))
        with open_store(config, readonly=True) as db:
            db.execute('BEGIN')
            current = plan(db, config, policy, digest, datetime.now(timezone.utc), request['inventory_run_id'],
                           request['inventory_plan_sha256'], request['approval_id'])
        if current != request or not request['candidates']:
            raise ValueError('Purge request no longer matches the current eligible quarantine')
    except (ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(verification['outcome_token'])
    print(f"{verification['plan_hash_output_token']} {current['purge_plan_sha256']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
