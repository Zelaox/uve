"""Evaluate the recorded parallel-validation window before primary reads."""
from __future__ import annotations

from datetime import datetime
import json
from uuid import uuid4

from .uve_triage_store import json_text, sha256


def record_reconciliation(db, report):
    checked = report['reconciliation']
    ok = (not report['errors'] and not checked['unmatched_sources'] and not checked['missing_proposal_ids']
          and checked['integrity_check'] == 'ok' and checked['run_count'] == checked['summary_count'])
    compact = {key: value for key, value in checked.items() if key not in ('unmatched_sources', 'missing_proposal_ids')}
    compact['unmatched_count'] = len(checked['unmatched_sources'])
    compact['missing_proposal_count'] = len(checked['missing_proposal_ids'])
    identifier = str(uuid4())
    db.execute('INSERT INTO migration_reconciliations VALUES(?,?,?,?,?)',
               (identifier, report['observed_at'], int(ok), sha256(json_text(report).encode()), json_text(compact)))
    return identifier


def cutover_status(db, config):
    policy = config.policy('adapter_ref')
    rows = db.execute('SELECT * FROM migration_reconciliations ORDER BY captured_at,reconciliation_id').fetchall()
    passed = []
    for row in rows:
        if row['ok']:
            passed.append(row)
        else:
            passed.clear()
    days = ((datetime.fromisoformat(passed[-1]['captured_at']) -
             datetime.fromisoformat(passed[0]['captured_at'])).total_seconds() / 86400) if passed else 0
    eligible = bool(passed) and days >= policy['dual_write_minimum_days']
    return {'status': 'eligible' if eligible else 'not_verified', 'required_days': policy['dual_write_minimum_days'],
            'observed_parallel_days': days, 'successful_reconciliations': len(passed),
            'first_success_at': passed[0]['captured_at'] if passed else None,
            'last_success_at': passed[-1]['captured_at'] if passed else None,
            'read_primary': policy['read_primary']}


def primary_advisor_snapshot(db, config):
    if cutover_status(db, config)['status'] != 'eligible':
        raise ValueError('Primary reads require the recorded parallel-validation window')
    row = db.execute("SELECT * FROM advisor_snapshots WHERE source_kind='advisor_sqlite' ORDER BY captured_at DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError('The primary advisor snapshot is unavailable')
    return dict(row)
