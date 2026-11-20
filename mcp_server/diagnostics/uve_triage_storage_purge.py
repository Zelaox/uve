"""Bind the final purge set to a fresh inventory and the independent UVE gate."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4

from .uve_triage_proof import validate_gate
from .uve_triage_sources import pipeline_document
from .uve_triage_storage_activity import require_mode
from .uve_triage_storage_execute import _execute_audit, _inventory
from .uve_triage_storage_lease import protected_leases, under
from .uve_triage_storage_scan import _references, classify, file_identity
from .uve_triage_store import json_text, open_store, sha256, stable_id, transaction, utc_now, within


def plan(db, config, policy, digest, now, inventory_id, inventory_hash, approval_id):
    from .uve_triage_storage_lifecycle import assert_current_policy

    assert_current_policy(config, digest)
    _inventory(db, policy, digest, now, inventory_id, inventory_hash)
    state = require_mode(db, policy, digest, now, 'purge', approval_id, inventory_id, persist=False)
    if state['paused']:
        raise ValueError('Purge is paused')
    manifest = policy['manifest']
    refs, holds = _references(db, config, policy)
    leases = [row['work_root_uri_relative'] for row in protected_leases(db, policy, now)]
    rows = db.execute('''SELECT a.*,c.content_sha256,c.byte_size,c.data_class_ref FROM cleanup_actions a
                         JOIN cleanup_candidates c USING(candidate_id)
                         JOIN cleanup_runs r ON r.cleanup_run_id=a.cleanup_run_id
                         WHERE a.action_kind='quarantined' AND r.aborted=0
                         ORDER BY a.action_id''')
    hold = timedelta(days=policy['policy']['durations_days']['quarantine_hold'])
    candidates = []
    for row in rows:
        if datetime.fromisoformat(row['executed_at']) + hold > now:
            continue
        source, destination = row['source_uri'], row['dest_uri']
        route = next((item for item in policy['scope']['roots'] if under(source, item['relative'])), None)
        if (not route or classify(config, policy, source, route)[0] != 'quarantine'
                or route['data_class_ref'] != row['data_class_ref']
                or not under(destination, manifest['quarantine_relative'])):
            raise ValueError('Quarantine ledger contains an out-of-scope path')
        if (any(under(source, path) or under(destination, path) for path in refs)
                or any(under(source, path) or under(destination, path) for path in (*holds, *leases))):
            continue
        metadata, file_hash = file_identity(config.path(destination), manifest['hash_chunk_bytes'])
        if file_hash != row['content_sha256'] or metadata.st_size != row['byte_size']:
            raise ValueError('Quarantine bytes no longer match the action receipt')
        candidates.append({'action_id': row['action_id'], 'dest_uri': destination, 'content_sha256': file_hash,
                           'byte_size': row['byte_size'], 'source_uri': source, 'data_class_ref': row['data_class_ref']})
    request = {'inventory_run_id': inventory_id, 'inventory_plan_sha256': inventory_hash,
               'approval_id': approval_id, 'manifest_sha256': digest, 'candidates': candidates}
    return request | {'purge_plan_sha256': stable_id(request)}


def _match_gate(config, policy, receipt_path, purge_hash):
    gate = validate_gate(config, receipt_path)
    verification = policy['verification']
    if gate['gate_id'] != verification['gate_id']:
        raise ValueError('Gate is for a different operation')
    ingest = config.policy('ingest_ref')
    log_root = pipeline_document(config, ingest['uve_log_root_ref_in_pipeline'])['log_dir']
    matches = []
    for ref in gate['probe_receipt_refs']:
        probe_path = within(config.pipeline, Path(log_root) / 'probes' / f"{ref['probe_run_id']}.json")
        receipt = json.loads(probe_path.read_bytes())
        canonical = {key: value for key, value in receipt.items() if not key.startswith('_')}
        stdout = receipt.get('observation', {}).get('stdout', '').splitlines()
        expected = f"{verification['plan_hash_output_token']} {purge_hash}"
        if (sha256(json_text(canonical).encode()) == ref['receipt_sha256']
                and receipt['verdict'] == 'passed'
                and receipt['probe']['probe_id'] == verification['probe_id']
                and receipt.get('profile_ref') == verification['profile_ref_in_pipeline']
                and expected in stdout):
            matches.append(receipt['probe_run_id'])
    if len(matches) != 1:
        raise ValueError('Independent gate did not verify this purge plan')
    return gate['trust_chain_ref']


def purge(config, policy, digest, now, inventory_id, inventory_hash, approval_id, gate_path, actor):
    from .uve_triage_storage_lifecycle import assert_current_policy

    run_id = str(uuid4())
    total = 0
    error = None
    gate_ref = None
    with open_store(config) as db:
        with transaction(db):
            _execute_audit(db, run_id, 'purge', actor, digest, inventory_hash, now)
        try:
            if gate_path is None:
                raise ValueError('Purge requires the independent UVE gate receipt')
            with transaction(db):
                reviewed = plan(db, config, policy, digest, now, inventory_id, inventory_hash, approval_id)
                db.execute('UPDATE cleanup_runs SET plan_sha256=? WHERE cleanup_run_id=?',
                           (reviewed['purge_plan_sha256'], run_id))
            gate_ref = _match_gate(config, policy, gate_path, reviewed['purge_plan_sha256'])
            with transaction(db):
                stamp = datetime.fromisoformat(utc_now())
                fresh = plan(db, config, policy, digest, stamp, inventory_id, inventory_hash, approval_id)
                if fresh != reviewed:
                    raise ValueError('Purge inventory changed after the gate verified it')
                for candidate in fresh['candidates']:
                    cid = stable_id(run_id, candidate['dest_uri'])
                    db.execute('INSERT INTO cleanup_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                               (cid, run_id, candidate['dest_uri'], 'lab', candidate['data_class_ref'], 0,
                                candidate['byte_size'], candidate['content_sha256'], 0, '[]', 'quarantine',
                                'expired_verified_quarantine'))
                    db.execute('INSERT INTO cleanup_actions VALUES(?,?,?,?,?,?,?,?,?)',
                               (stable_id(run_id, candidate['action_id']), run_id, cid, 'purge_prepared', None,
                                candidate['dest_uri'], None, 0,
                                json_text({'gate_ref': gate_ref, 'prior_action_id': candidate['action_id'],
                                           'purge_plan_sha256': fresh['purge_plan_sha256']})))
            for candidate in reviewed['candidates']:
                with transaction(db):
                    current = datetime.fromisoformat(utc_now())
                    assert_current_policy(config, digest)
                    require_mode(db, policy, digest, current, 'purge', approval_id, inventory_id)
                    refs, holds = _references(db, config, policy)
                    leases = [row['work_root_uri_relative'] for row in protected_leases(db, policy, current)]
                    uris = (candidate['source_uri'], candidate['dest_uri'])
                    if any(any(under(uri, root) for root in (*refs, *holds, *leases)) for uri in uris):
                        raise ValueError('A reference or lease appeared after purge preparation')
                    path = config.path(candidate['dest_uri'])
                    if file_identity(path, policy['manifest']['hash_chunk_bytes'])[1] != candidate['content_sha256']:
                        raise ValueError('Purge candidate identity changed before unlink')
                    pending = stable_id(run_id, candidate['action_id'])
                    path.unlink()
                    db.execute("UPDATE cleanup_actions SET action_kind='purged',executed_at=? WHERE action_id=?",
                               (utc_now(), pending))
                    db.execute("UPDATE cleanup_actions SET action_kind='quarantine_purged' WHERE action_id=?",
                               (candidate['action_id'],))
                total += candidate['byte_size']
            if total and approval_id:
                with transaction(db):
                    db.execute('UPDATE inactivity_protection_state SET post_return_reconcile_required=0,'
                               'last_post_return_inventory_run_id=NULL WHERE state_id=?', (policy['manifest']['state_id'],))
        except (ValueError, OSError, KeyError, sqlite3.Error, subprocess.SubprocessError) as failure:
            error = str(failure)
        with transaction(db):
            db.execute('UPDATE cleanup_runs SET finished_at=?,bytes_purged=?,aborted=?,abort_reason=? WHERE cleanup_run_id=?',
                       (utc_now(), total, int(bool(error)), error, run_id))
    return {'cleanup_run_id': run_id, 'aborted': bool(error), 'abort_reason': error,
            'bytes_purged': total, 'gate_ref': gate_ref}
