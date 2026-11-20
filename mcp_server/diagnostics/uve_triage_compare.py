"""Materialize comparisons and keep their original policy identity."""
from __future__ import annotations

from .uve_triage_store import json_text, sha256, stable_id, utc_now
from .uve_triage_proof import current_resolution


def _counts(db, run_id):
    return dict(db.execute('''SELECT f.finding_fingerprint,SUM(o.count) FROM finding_occurrences o
                             JOIN findings f USING(finding_id) WHERE o.run_id=? GROUP BY f.finding_fingerprint''', (run_id,)))


def compare_runs(db, config, a: str, b: str, *, advance: bool = False) -> dict:
    if a == b:
        raise ValueError('A comparison needs two different runs')
    rows = [db.execute('SELECT * FROM runs WHERE run_id=?', (run,)).fetchone() for run in (a, b)]
    if any(row is None for row in rows) or rows[0]['project_id'] != rows[1]['project_id']:
        raise ValueError('Both runs must exist in the same project')
    left, right = rows
    ref = config.spec['comparison_ref']
    policy = config.document(ref)
    allowed = config.document(policy['catalog_ref'])
    digest = sha256(config.path(ref).read_bytes())
    identifier = stable_id(a, b, digest)
    if db.execute('SELECT 1 FROM run_comparisons WHERE comparison_id=?', (identifier,)).fetchone():
        return read_comparison(db, identifier)
    count_a, count_b = _counts(db, a), _counts(db, b)
    items = []
    for fingerprint in sorted(count_a.keys() | count_b.keys()):
        before, after = count_a.get(fingerprint, 0), count_b.get(fingerprint, 0)
        finding = db.execute('SELECT status,first_seen_at FROM findings WHERE finding_fingerprint=?', (fingerprint,)).fetchone()
        state = finding['status']
        gate = None
        if not after:
            gate = current_resolution(db, config, fingerprint, left['started_at'], right['started_at'])
            category = lifecycle = 'resolved' if gate else 'not_observed'
        elif state == 'resolved' and current_resolution(db, config, fingerprint, finding['first_seen_at'], right['started_at']):
            category = lifecycle = 'regressed'
        elif not before:
            category, lifecycle = 'new', 'new' if state == 'new' else 'investigating'
        else:
            category = 'increased' if after - before > policy['increased_delta'] else 'recurring'
            lifecycle = 'regressed' if state == 'regressed' else 'investigating'
        if category not in allowed['comparison_categories'] or lifecycle not in allowed['states']:
            raise ValueError('Comparison produced a state outside the manifest catalog')
        items.append((fingerprint, category, before, after, after-before, lifecycle, gate))
    db.execute('INSERT INTO run_comparisons VALUES(?,?,?,?,?,?,?,?)',
               (identifier, left['project_id'], a, b, utc_now(), digest, ref, stable_id(items)))
    for item in items:
        db.execute('INSERT INTO run_comparison_items VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (stable_id(identifier, item[0]), identifier, *item, json_text({})))
        if advance:
            db.execute('UPDATE findings SET status=? WHERE finding_fingerprint=?', (item[5], item[0]))
    if advance:
        db.execute('UPDATE run_summaries SET regression_status=? WHERE run_id=?',
                   (json_text({category: sum(i[1] == category for i in items) for category in sorted({i[1] for i in items})}), b))
    return read_comparison(db, identifier)


def read_comparison(db, identifier: str) -> dict:
    record = db.execute('SELECT * FROM run_comparisons WHERE comparison_id=?', (identifier,)).fetchone()
    if record is None:
        raise ValueError('Unknown comparison')
    items = db.execute('SELECT * FROM run_comparison_items WHERE comparison_id=? ORDER BY finding_fingerprint', (identifier,))
    return {**dict(record), 'items': [dict(item) for item in items]}
