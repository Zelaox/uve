"""Use recorded Lab activity to gate lifecycle mutations and bind return approval."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from uuid import uuid4

from .uve_triage_store import json_text, stable_id


def _tier(policy, now, last):
    config = policy['inactivity_tiers']
    if last is None:
        return next(row for row in config['tiers'] if row['tier_ref'] == config['no_activity_tier'])
    age = (now - datetime.fromisoformat(last)).total_seconds() / 86400
    if age < 0:
        raise ValueError('Activity timestamp is in the future')
    return max((row for row in config['tiers'] if row['minimum_days'] <= age), key=lambda row: row['minimum_days'])


def refresh_state(db, policy, digest, now, *, persist=True):
    state_id = policy['manifest']['state_id']
    old = db.execute('SELECT * FROM inactivity_protection_state WHERE state_id=?', (state_id,)).fetchone()
    last = db.execute('SELECT MAX(observed_at) FROM lab_activity_markers WHERE inactivity_clock_eligible=1').fetchone()[0]
    tier = _tier(policy, now, last)
    long_absence = policy['return_reconciliation']['long_absence_tier_ref']
    needs_return = bool(old and old['post_return_reconcile_required']) or tier['tier_ref'] == long_absence
    inventory = old['last_post_return_inventory_run_id'] if old else None
    if tier['tier_ref'] == long_absence or (old and old['manifest_sha256'] != digest):
        inventory = None
    paused = 'purge' not in tier['modes']
    since = (old['paused_since'] if old and old['paused_since'] else now.isoformat()) if paused else None
    reason = 'post_return_reconciliation' if needs_return else ('inactive_tier' if paused else 'active')
    state = {'state_id': state_id, 'paused': int(paused), 'paused_since': since, 'last_activity_at': last,
             'inactivity_tier_ref': tier['tier_ref'], 'reason_ref': reason, 'manifest_sha256': digest,
             'post_return_reconcile_required': int(needs_return), 'last_post_return_inventory_run_id': inventory}
    if persist:
        db.execute('INSERT OR REPLACE INTO inactivity_protection_state VALUES(?,?,?,?,?,?,?,?,?)', tuple(state.values()))
    return state | {'allowed_modes': tier['modes']}


def record_activity(db, policy, digest, now, kind, actor, source):
    activity = policy['activity']
    if actor not in activity['kinds'].get(kind, []):
        raise ValueError('Activity kind does not permit that actor class')
    eligible = actor in activity['eligible_actor_classes']
    stamp = now.isoformat()
    if eligible:
        # Detect the elapsed absence before inserting the returning operator's marker.
        last = db.execute('SELECT MAX(observed_at) FROM lab_activity_markers WHERE inactivity_clock_eligible=1').fetchone()[0]
        if last is not None:
            refresh_state(db, policy, digest, now)
    db.execute('INSERT INTO lab_activity_markers VALUES(?,?,?,?,?,?,?)',
               (str(uuid4()), kind, stamp, source, actor, int(eligible), stable_id(kind, stamp, source, actor)))
    return refresh_state(db, policy, digest, now)


def approve_return(db, policy, digest, now, inventory_id, approver):
    state = refresh_state(db, policy, digest, now)
    row = db.execute('SELECT * FROM cleanup_runs WHERE cleanup_run_id=?', (inventory_id,)).fetchone()
    returning = policy['return_reconciliation']
    if (not approver or not state['post_return_reconcile_required']
            or inventory_id != state['last_post_return_inventory_run_id'] or not row or row['aborted']
            or row['manifest_sha256'] != digest or row['reconciliation_kind'] != returning['reconciliation_kind']
            or datetime.fromisoformat(row['finished_at']) + timedelta(seconds=returning['inventory_valid_seconds']) < now):
        raise ValueError('A fresh post-return inventory is required')
    approval = str(uuid4())
    expires = now + timedelta(seconds=returning['approval_expiry_seconds'])
    db.execute('INSERT INTO lifecycle_purge_approvals VALUES(?,?,?,?,?,?,?)',
               (approval, now.isoformat(), approver, inventory_id, digest, expires.isoformat(),
                json_text({'plan_sha256': row['plan_sha256']})))
    return {'approval_id': approval, 'inventory_run_id': inventory_id, 'expires_at': expires.isoformat()}


def require_mode(db, policy, digest, now, mode, approval_id=None, inventory_id=None, *, persist=True):
    if not policy['manifest']['enabled']:
        raise ValueError('Lifecycle execution is disabled by the manifest')
    state = refresh_state(db, policy, digest, now, persist=persist)
    if mode not in state['allowed_modes']:
        raise ValueError('The inactivity tier permits inventory only')
    if state['post_return_reconcile_required']:
        if mode not in policy['return_reconciliation']['require_approval_modes']:
            raise ValueError('Post-return reconciliation is pending')
        approval = db.execute('''SELECT a.*,r.plan_sha256,r.finished_at FROM lifecycle_purge_approvals a
                                 JOIN cleanup_runs r ON r.cleanup_run_id=a.post_return_inventory_cleanup_run_id
                                 WHERE a.approval_id=?''', (approval_id,)).fetchone()
        if (not approval or approval['manifest_sha256'] != digest
                or approval['post_return_inventory_cleanup_run_id'] != state['last_post_return_inventory_run_id']
                or approval['post_return_inventory_cleanup_run_id'] != inventory_id
                or datetime.fromisoformat(approval['expires_at']) < now
                or datetime.fromisoformat(approval['finished_at']) +
                timedelta(seconds=policy['return_reconciliation']['inventory_valid_seconds']) < now
                or json.loads(approval['detail_json'])['plan_sha256'] != approval['plan_sha256']):
            raise ValueError('Execution requires current post-return approval')
    return state
