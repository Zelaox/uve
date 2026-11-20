"""Invoke the index writer after authoritative writers have finished."""
from __future__ import annotations

import json
import logging
import subprocess
import sys

from .uve_triage_store import load_config


def index_sidecar(root, purpose, report_path=None):
    try:
        config = load_config(root)
        policy = config.policy('adapter_ref')
        command = [sys.executable, '-B', '-m', 'mcp_server.diagnostics.uve_triage_ingest', '--batch', purpose]
        if report_path is not None:
            if not policy['engine_index_opt_in']:
                return {'status': 'disabled'}
            command.extend(['--source-type', policy['engine_session_source_type'], '--source-path', str(report_path)])
        elif purpose == 'advisor_mirror' and policy['mirror_advisor_writes']:
            for source_type in policy['advisor_source_types']:
                command.extend(['--source-type', source_type])
        else:
            return {'status': 'disabled'}
        process = subprocess.run(command, cwd=root, capture_output=True, encoding='utf-8',
                                 timeout=policy['index_timeout_seconds'], check=False)
        if process.stdout:
            result = json.loads(process.stdout)
            return {'status': 'indexed' if process.returncode == 0 else 'not_verified', **result}
        raise ValueError('Index subprocess returned no batch result')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        logging.getLogger(__name__).warning('UVE index sidecar failed: %s', type(error).__name__)
        return {'status': 'not_verified', 'error_class': type(error).__name__}
