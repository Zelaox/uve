"""Consume current independent UVE receipts through the configured engine."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from .uve_triage_sources import identifier, pipeline_document
from .uve_triage_store import json_text, sha256, utc_now, within


def evidence_document(db, config, evidence_id):
    row = db.execute('SELECT * FROM evidence_refs WHERE evidence_id=?', (evidence_id,)).fetchone()
    if not row:
        raise ValueError('Unknown evidence reference')
    root = {'lab': config.root, 'pipeline': config.pipeline}[row['root_kind']]
    path = within(root, row['uri_relative'])
    if path.stat().st_size != row['byte_size']:
        raise ValueError('Indexed evidence size changed')
    raw = path.read_bytes()
    if sha256(raw) != row['content_sha256']:
        raise ValueError('Indexed evidence hash changed')
    return row, json.loads(raw)


def validate_gate(config, path, expected_sha256=None):
    ingest, proof = config.policy('ingest_ref'), config.policy('proof_ref')
    log_root = pipeline_document(config, ingest['uve_log_root_ref_in_pipeline'])['log_dir']
    root = within(config.pipeline, Path(log_root) / 'gates')
    path = within(root, Path(path).relative_to(root))
    raw = path.read_bytes()
    digest = sha256(raw)
    if expected_sha256 and digest != expected_sha256:
        raise ValueError('Gate receipt bytes changed')
    gate = json.loads(raw)
    if (gate.get('decision') != 'approved' or gate.get('gate_evaluator_ref') not in proof['allowed_evaluator_refs']
            or not identifier(gate.get('trust_chain_ref'))):
        raise ValueError('Gate is missing independent approval')
    registered = pipeline_document(config, proof['gate_manifest_ref_in_pipeline'])['gates'].get(gate.get('gate_id'))
    refs = gate.get('probe_receipt_refs', [])
    if not registered or not refs or any(identifier(ref.get('probe_run_id')) != ref.get('probe_run_id') for ref in refs):
        raise ValueError('Gate references an unknown probe or gate identity')
    binding = config.policy('lab_binding_ref')
    command = (
        'import json,sys; from agent_training.uve.gate_evaluator import validate_stored_gate; '
        'raise SystemExit(0 if validate_stored_gate(json.load(sys.stdin)) else 1)'
    )
    process = subprocess.run([sys.executable, '-B', '-c', command], input=raw, cwd=config.pipeline,
                             env={**os.environ, binding['lab_root_env_key']: str(config.root),
                                  'PYTHONPATH': str(config.pipeline)},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                             timeout=proof['timeout_seconds'])
    if process.returncode or sha256(path.read_bytes()) != digest:
        raise ValueError('UVE rejected the current gate receipt')
    return gate


def link_indexed_gates(db, config, evidence_ids):
    binding = config.policy('lab_binding_ref')
    proof = config.policy('proof_ref')
    for evidence_id in evidence_ids:
        row, document = evidence_document(db, config, evidence_id)
        gate_id = identifier(document.get('gate_id'))
        decision = identifier(document.get('decision'))
        if not gate_id or not decision:
            continue
        linked = []
        for ref in document.get('probe_receipt_refs', []):
            probe = db.execute('SELECT * FROM probe_results WHERE probe_run_id=?', (ref['probe_run_id'],)).fetchone()
            if probe:
                linked.append(probe)
                db.execute('INSERT OR IGNORE INTO gate_links VALUES(?,?,?,?,?,?)',
                           (probe['probe_run_id'], evidence_id, gate_id, decision, 0, utc_now()))
        if not linked or decision != 'approved' or document.get('gate_evaluator_ref') not in proof['allowed_evaluator_refs']:
            continue
        try:
            gate = validate_gate(config, within(config.pipeline, row['uri_relative']), row['content_sha256'])
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
        hashes = {item['probe_run_id']: item['receipt_sha256'] for item in gate['probe_receipt_refs']}
        gate_spec = pipeline_document(config, proof['gate_manifest_ref_in_pipeline'])['gates'][gate_id]
        for probe in linked:
            if probe['probe_ref'] not in gate_spec['required_probe_ids']:
                continue
            try:
                _, receipt = evidence_document(db, config, probe['receipt_ref'])
            except (ValueError, OSError):
                continue
            canonical = {key: value for key, value in receipt.items() if not key.startswith('_')}
            if receipt.get('verdict') != 'passed' or sha256(json_text(canonical).encode()) != hashes[probe['probe_run_id']]:
                continue
            db.execute('UPDATE gate_links SET checked=1 WHERE probe_run_id=? AND gate_receipt_ref=?',
                       (probe['probe_run_id'], evidence_id))
            db.execute('UPDATE probe_results SET outcome=?,gate_receipt_ref=?,verified_at=? WHERE probe_run_id=?',
                       ('pass', evidence_id, utc_now(), probe['probe_run_id']))
            if gate_id == binding['validate_gate_id']:
                _link_bundle(db, proof, receipt, probe['run_id'], evidence_id)
            _link_resolution(db, proof, receipt, probe, gate_id, evidence_id)


def _link_bundle(db, proof, receipt, probe_run_id, gate_evidence_id):
    prefix = proof['bundle_hash_output_token'] + ' '
    stdout = receipt.get('observation', {}).get('stdout', '')
    for line in stdout.splitlines():
        if line.startswith(prefix):
            digest = line.removeprefix(prefix)
            for row in db.execute('''SELECT r.run_id FROM runs r JOIN run_evidence re USING(run_id)
                                     JOIN evidence_refs e USING(evidence_id)
                                     WHERE r.run_kind='bundle_build' AND e.content_sha256=?''', (digest,)):
                db.execute('INSERT OR IGNORE INTO run_links VALUES(?,?,?)',
                           (row[0], probe_run_id, 'bundle_validation_probe'))
                db.execute('INSERT OR IGNORE INTO run_evidence VALUES(?,?,?)',
                           (row[0], gate_evidence_id, 'independent_gate'))


def _matching_fingerprints(db, policy, receipt, probe, gate_id):
    observation = receipt.get('observation', {})
    subject = receipt.get('subject_identity', {}).get('project_id')
    for contract in policy['resolution_contracts']:
        if (contract['project_id'], contract['gate_id'], contract['probe_ref']) != (subject, gate_id, probe['probe_ref']):
            continue
        if receipt.get('profile_ref') != contract['profile_ref']:
            continue
        if any(observation.get(key) != value for key, value in contract.get('required_observations', {}).items()):
            continue
        lines = observation.get('stdout', '').splitlines()
        if any(line not in lines for line in contract.get('required_stdout_lines', [])):
            continue
        field = contract.get('fingerprint_field')
        if not field and not (contract.get('required_observations') or contract.get('required_stdout_lines')):
            continue
        fingerprint = identifier(observation.get(field) if field else contract.get('fingerprint'))
        if not fingerprint:
            continue
        match = db.execute('''SELECT 1 FROM findings JOIN runs USING(run_id)
                              WHERE finding_fingerprint=? AND project_id=? AND diagnostic_code=?''',
                           (fingerprint, subject, contract['diagnostic_code'])).fetchone()
        if match:
            yield fingerprint


def _link_resolution(db, policy, receipt, probe, gate_id, evidence_id):
    for fingerprint in _matching_fingerprints(db, policy, receipt, probe, gate_id):
        db.execute('INSERT OR IGNORE INTO resolution_links VALUES(?,?,?,?)',
                   (fingerprint, probe['probe_run_id'], evidence_id, utc_now()))


def current_resolution(db, config, fingerprint, after, before):
    rows = db.execute('''SELECT rl.gate_receipt_ref,e.uri_relative,e.content_sha256,p.probe_ref,p.probe_run_id,p.receipt_ref
                         FROM resolution_links rl
                         JOIN gate_links gl USING(probe_run_id,gate_receipt_ref)
                         JOIN probe_results p ON p.probe_run_id=rl.probe_run_id
                         JOIN evidence_refs e ON e.evidence_id=rl.gate_receipt_ref
                         WHERE rl.finding_fingerprint=? AND gl.checked=1 AND e.root_kind='pipeline'
                         AND rl.verified_at>=? AND rl.verified_at<=? ORDER BY rl.verified_at DESC''',
                      (fingerprint, after, before)).fetchall()
    for row in rows:
        try:
            gate = validate_gate(config, within(config.pipeline, row['uri_relative']), row['content_sha256'])
            _, receipt = evidence_document(db, config, row['receipt_ref'])
            if fingerprint in _matching_fingerprints(db, config.policy('proof_ref'), receipt, row, gate['gate_id']):
                return row['gate_receipt_ref']
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
    return None
