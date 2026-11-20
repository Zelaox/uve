"""Opt-in sidecar for receipts belonging to manifest-bound Lab projects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agent_training.manifest_loader import load_json_ref, pipeline_root
from agent_training.uve.bundle import resolve_log_dir


def index_receipts(probes, gate=None):
    try:
        binding = load_json_ref('config/uve/ai_lab_roots.json')
        root = Path(os.environ[binding['ai_lab_root_env']]).resolve(strict=True)
        storage = json.loads((root / binding['observability_storage_ref_from_lab']).read_bytes())
        bound_pipeline = json.loads((root / storage['lab_binding_ref']).read_bytes())['training_pipeline_relative_from_repo']
        if (root / bound_pipeline).resolve() != pipeline_root():
            raise ValueError('Lab root is bound to a different pipeline')
        adapter = json.loads((root / storage['adapter_ref']).read_bytes())
        allowed = adapter['uve_receipt_project_ids']
        if not adapter['uve_receipt_sidecar_enabled'] or not probes:
            return
        if not all(probe['subject_identity']['project_id'] in allowed for probe in probes):
            raise ValueError('Receipt project is outside the Lab sidecar policy')
        logs = resolve_log_dir()
        args = [sys.executable, '-B', '-m', 'mcp_server.diagnostics.uve_triage_ingest',
                '--batch', 'uve-receipt-sidecar', '--actor-class', adapter['uve_receipt_actor_class'],
                '--source-type', adapter['uve_probe_source_type']]
        for probe in probes:
            args.extend(['--source-path', str(logs / 'probes' / f"{probe['probe_run_id']}.json")])
        if gate:
            args.extend(['--source-type', adapter['uve_gate_source_type'],
                         '--source-path', str(logs / 'gates' / f"{gate['trust_chain_ref']}.json")])
        output = subprocess.run(args, cwd=root, capture_output=True, timeout=adapter['index_timeout_seconds'], check=False)
        if output.returncode:
            raise ValueError(f'Index sidecar exit code {output.returncode}')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f'UVE observability index not_verified ({type(error).__name__})', file=sys.stderr)
