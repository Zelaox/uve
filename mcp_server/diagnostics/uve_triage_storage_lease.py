"""Protect active work until a lease is released or expiry and liveness agree."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from .uve_triage_sources import pipeline_document
from .uve_triage_store import json_text, open_store, transaction, utc_now, within


def under(path, root):
    path, root = path.casefold(), root.casefold().rstrip('/')
    return path == root or path.startswith(root + '/')


def _process_created(pid):
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def protected_leases(db, policy, now):
    grace = timedelta(seconds=policy['active_work']['post_expiry_grace_seconds'])
    for row in db.execute('SELECT * FROM active_run_leases WHERE released_at IS NULL'):
        if now <= datetime.fromisoformat(row['expires_at']) + grace:
            yield row
            continue
        try:
            running = _process_created(row['pid'])
            if running is not None and (row['process_created_at'] is None or running == row['process_created_at']):
                yield row
        except Exception:
            # Permission and liveness uncertainty keep the work protected.
            yield row


def _receipt(config, relative, payload):
    path = within(config.pipeline, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.{uuid4().hex}.tmp')
    temporary.write_text(json_text(payload), encoding='utf-8')
    os.replace(temporary, path)


def lease(config, policy, operation, *, lease_id=None, work_root=None, actor=None, pid=None, linked_run=None):
    active = policy['active_work']
    now = utc_now()
    expires = (datetime.fromisoformat(now) + timedelta(seconds=active['lease_ttl_seconds'])).isoformat()
    with open_store(config) as db, transaction(db):
        if operation == 'acquire':
            if actor not in active['actor_refs'] or type(pid) is not int or pid < 1:
                raise ValueError('Lease requires a configured actor and writer PID')
            root = config.path(work_root).relative_to(config.root).as_posix()
            permitted = [item['relative'] for item in policy['scope']['roots']] + active['lease_root_prefixes']
            if not any(under(root, item) for item in permitted):
                raise ValueError('Lease root falls outside the configured work roots')
            if any(part.casefold() in root.casefold() for part in policy['scope']['excluded_path_parts']):
                raise ValueError('Lease root matches a scope exclusion')
            lock = pipeline_document(config, active['resource_lock_ref_in_pipeline'])
            created = _process_created(pid)
            if created is None:
                raise ValueError('Writer PID is no longer active')
            lease_id = str(uuid4())
            receipt = (Path(lock['lock_store_relative']) / active['resource_kind_ref'] / f'{lease_id}.json').as_posix()
            db.execute('INSERT INTO active_run_leases VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
                       (lease_id, active['resource_kind_ref'], actor, root, '[]', linked_run, now,
                        expires, receipt, pid, created))
        else:
            row = db.execute('SELECT * FROM active_run_leases WHERE lease_id=?', (lease_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown lease')
            if operation == 'release':
                db.execute('UPDATE active_run_leases SET released_at=COALESCE(released_at,?) WHERE lease_id=?', (now, lease_id))
            elif operation == 'heartbeat':
                if row['released_at'] or _process_created(row['pid']) != row['process_created_at']:
                    raise ValueError('Lease writer is no longer active')
                db.execute('UPDATE active_run_leases SET heartbeat_at=?,expires_at=? WHERE lease_id=?', (now, expires, lease_id))
            else:
                raise ValueError('Unknown lease operation')
        result = dict(db.execute('SELECT * FROM active_run_leases WHERE lease_id=?', (lease_id,)).fetchone())
        _receipt(config, result['lease_store_receipt_ref'], result)
        return result
