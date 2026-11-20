"""Apply a reviewed inventory with durable action intents and fresh eligibility checks."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from uuid import uuid4

from .uve_triage_storage_activity import require_mode
from .uve_triage_storage_scan import analyze, file_identity
from .uve_triage_store import json_text, open_store, stable_id, transaction, utc_now


def _inventory(db, policy, digest, now, inventory_id, plan_digest):
    row = db.execute('SELECT * FROM cleanup_runs WHERE cleanup_run_id=?', (inventory_id,)).fetchone()
    valid_for = timedelta(seconds=policy['return_reconciliation']['inventory_valid_seconds'])
    if (not row or row['mode'] != policy['manifest']['default_mode'] or row['aborted']
            or row['manifest_sha256'] != digest or row['plan_sha256'] != plan_digest
            or datetime.fromisoformat(row['finished_at']) + valid_for < now):
        raise ValueError('Execution requires a current dry-run inventory and its plan hash')
    return row


def _quarantine_plan(db, config, policy, now, inventory_id):
    candidates = db.execute("SELECT * FROM cleanup_candidates WHERE cleanup_run_id=? AND decision='quarantine'",
                            (inventory_id,)).fetchall()
    fresh = {row['uri_relative']: row for row in analyze(db, config, policy, now)}
    for candidate in candidates:
        current = fresh.get(candidate['uri_relative'], {})
        if current.get('decision') != 'quarantine' or current.get('content_sha256') != candidate['content_sha256']:
            raise ValueError('Candidate changed or is now protected. Run a fresh inventory.')
    return candidates


def _copy_candidate(db, run_id, candidate):
    values = dict(candidate)
    values.pop('candidate_id')
    values.pop('cleanup_run_id')
    cid = stable_id(run_id, values['uri_relative'])
    db.execute('INSERT INTO cleanup_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (cid, run_id, *values.values()))
    return cid


def _execute_audit(db, run_id, mode, actor, digest, plan_digest, now):
    db.execute('''INSERT INTO cleanup_runs(cleanup_run_id,mode,actor_class_ref,started_at,finished_at,
                  manifest_sha256,bytes_scanned,bytes_reclaimable,protected_count,aborted,abort_reason,
                  plan_sha256,storage_health_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (run_id, mode, actor, now.isoformat(), now.isoformat(), digest, 0, 0, 0, 1, 'execution_pending',
                plan_digest, '{}'))


def quarantine(config, policy, digest, now, inventory_id, plan_digest, approval_id, actor):
    from .uve_triage_storage_lifecycle import assert_current_policy

    run_id = str(uuid4())
    destination = config.path(policy['manifest']['quarantine_relative'])
    moved = 0
    error = None
    with open_store(config) as db:
        with transaction(db):
            _execute_audit(db, run_id, 'quarantine', actor, digest, plan_digest, now)
        try:
            with transaction(db):
                _inventory(db, policy, digest, now, inventory_id, plan_digest)
                assert_current_policy(config, digest)
                require_mode(db, policy, digest, now, 'quarantine', approval_id, inventory_id)
                candidates = _quarantine_plan(db, config, policy, now, inventory_id)
                actions = []
                for candidate in candidates:
                    cid = _copy_candidate(db, run_id, candidate)
                    target = (destination / (uuid4().hex + Path(candidate['uri_relative']).suffix)).relative_to(config.root).as_posix()
                    aid = str(uuid4())
                    db.execute('INSERT INTO cleanup_actions VALUES(?,?,?,?,?,?,?,?,?)',
                               (aid, run_id, cid, 'quarantine_prepared', None, candidate['uri_relative'],
                                target, 1, json_text({'content_sha256': candidate['content_sha256']})))
                    actions.append((aid, candidate, target))
            if actions:
                destination.mkdir(parents=True, exist_ok=True)
            for aid, candidate, target in actions:
                with transaction(db):
                    # Commit completed moves individually so a later failure retains their audit trail.
                    assert_current_policy(config, digest)
                    stamp = datetime.fromisoformat(utc_now())
                    require_mode(db, policy, digest, stamp, 'quarantine', approval_id, inventory_id)
                    fresh = {row['uri_relative']: row for row in analyze(db, config, policy, stamp)}
                    current = fresh.get(candidate['uri_relative'], {})
                    if current.get('decision') != 'quarantine' or current.get('content_sha256') != candidate['content_sha256']:
                        raise ValueError('Candidate acquired a reference or active work lease')
                    source, dest = config.path(candidate['uri_relative']), config.path(target)
                    if dest.exists() or file_identity(source, policy['manifest']['hash_chunk_bytes'])[1] != candidate['content_sha256']:
                        raise ValueError('Quarantine identity changed before move')
                    source.rename(dest)
                    if file_identity(dest, policy['manifest']['hash_chunk_bytes'])[1] != candidate['content_sha256']:
                        raise ValueError('Quarantined bytes differ from prepared intent')
                    db.execute("UPDATE cleanup_actions SET action_kind='quarantined',executed_at=? WHERE action_id=?",
                               (utc_now(), aid))
                moved += candidate['byte_size']
        except (ValueError, OSError, sqlite3.Error) as failure:
            error = str(failure)
        with transaction(db):
            db.execute('UPDATE cleanup_runs SET finished_at=?,bytes_quarantined=?,aborted=?,abort_reason=? WHERE cleanup_run_id=?',
                       (utc_now(), moved, int(bool(error)), error, run_id))
    return {'cleanup_run_id': run_id, 'aborted': bool(error), 'abort_reason': error, 'bytes_quarantined': moved}


def compact(config, policy, digest, now, approval_id, actor):
    from .uve_triage_storage_lifecycle import assert_current_policy

    retention = config.policy('retention_ref')
    if retention['row_pruning'] or not retention['retain_comparisons'] or retention['checkpoint_mode'] != 'PASSIVE':
        raise ValueError('The configured compaction must preserve indexed history')
    run_id = str(uuid4())
    error = None
    with open_store(config) as db:
        with transaction(db):
            _execute_audit(db, run_id, 'compact_sqlite', actor, digest, digest, now)
        try:
            with transaction(db):
                assert_current_policy(config, digest)
                require_mode(db, policy, digest, now, 'compact_sqlite', approval_id)
                if retention['require_no_active_holds'] and db.execute(
                        'SELECT 1 FROM evidence_holds WHERE released_at IS NULL LIMIT 1').fetchone():
                    raise ValueError('Active evidence holds prohibit compaction')
            checkpoint = tuple(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
        except (ValueError, OSError, sqlite3.Error) as failure:
            error, checkpoint = str(failure), None
        with transaction(db):
            db.execute('INSERT INTO compaction_runs VALUES(?,?,?,?,?)',
                       (run_id, now.isoformat(), utc_now(), 'failed' if error else 'complete',
                        json_text({'checkpoint': checkpoint, 'retention_ref': config.spec['retention_ref'], 'error': error})))
            db.execute('UPDATE cleanup_runs SET finished_at=?,aborted=?,abort_reason=? WHERE cleanup_run_id=?',
                       (utc_now(), int(bool(error)), error, run_id))
    return {'cleanup_run_id': run_id, 'aborted': bool(error), 'abort_reason': error, 'checkpoint': checkpoint}
