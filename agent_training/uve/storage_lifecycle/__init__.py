"""Bind UVE's storage command to the authorized AI Lab installation."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from agent_training.manifest_loader import load_json_ref


def dispatch(args):
    binding = load_json_ref('config/uve/ai_lab_roots.json')
    env_key = binding['ai_lab_root_env']
    if not os.environ.get(env_key):
        raise SystemExit(f'Set {env_key} to the bound AI Lab workspace')
    lab = Path(os.environ[env_key]).resolve(strict=True)
    sys.path.insert(0, str(lab))
    from mcp_server.diagnostics.uve_triage_store import load_config
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import main

    config = load_config(lab)
    if config.pipeline != Path(__file__).resolve().parents[3]:
        raise SystemExit('The Lab manifest is bound to a different UVE pipeline')
    return main(config, args.storage_argv)
