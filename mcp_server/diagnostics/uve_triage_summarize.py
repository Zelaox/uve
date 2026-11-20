"""Deterministic summaries linked to indexed evidence, including failed runs."""
from __future__ import annotations

from .uve_triage_store import json_text, stable_id, utc_now


def summarize_run(db, config, run_id: str) -> None:
    policy = config.policy('summary_ref')
    run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    findings = db.execute('''SELECT f.finding_id,f.diagnostic_code,COALESCE(o.severity,f.severity) AS severity,
                             f.entity_ref,SUM(o.count) AS n
                             FROM finding_occurrences o JOIN findings f USING(finding_id)
                             WHERE o.run_id=? GROUP BY f.finding_id,COALESCE(o.severity,f.severity)
                             ORDER BY n DESC,f.finding_id''', (run_id,)).fetchall()
    evidence = db.execute('SELECT DISTINCT evidence_id FROM run_evidence WHERE run_id=?', (run_id,)).fetchall()
    if not evidence:
        raise ValueError('A run summary requires a source evidence reference')
    counts = {bucket: sum(row['n'] for row in findings if row['severity'] in levels)
              for bucket, levels in policy['severity_buckets'].items()}
    classification = config.document(policy['classification_ref'])['mappings']
    actions = {item['lab_finding_code']: item.get('next_step_id') for item in classification}
    action = next((actions[row['diagnostic_code']] for row in findings if actions.get(row['diagnostic_code'])), None)
    schema_issues = sum(row['n'] for row in findings if row['diagnostic_code'].startswith(tuple(policy['schema_code_prefixes'])))
    status = run['status']
    if status in policy['unverified_statuses']:
        rollup = 'not_verified'
    elif status in policy['failure_statuses'] or counts['error']:
        rollup = 'failed'
    else:
        rollup = 'warning' if counts['warning'] else 'ok'
    token = policy['rollup'][rollup]
    template = config.path(policy['template_ref']).read_text(encoding='utf-8').strip()
    body = template.format(rollup=token, project_id=run['project_id'], evidence_count=len(evidence), **counts)
    if not body:
        raise ValueError('Summary template produced no text')
    db.execute('''INSERT INTO run_summaries(run_id,project_id,rollup_status,error_count,warning_count,info_count,
                  schema_issue_count,root_cause_status,most_affected_entity_ref,suggested_action_ref,summary_text,summary_generated_at,
                  generator_ref,summary_complete,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(run_id) DO UPDATE SET project_id=excluded.project_id,
                  rollup_status=excluded.rollup_status,error_count=excluded.error_count,
                  warning_count=excluded.warning_count,info_count=excluded.info_count,
                  schema_issue_count=excluded.schema_issue_count,most_affected_entity_ref=excluded.most_affected_entity_ref,
                  suggested_action_ref=excluded.suggested_action_ref,summary_text=excluded.summary_text,
                  summary_generated_at=excluded.summary_generated_at,
                  generator_ref=excluded.generator_ref,summary_complete=excluded.summary_complete,detail_json=excluded.detail_json''',
               (run_id, run['project_id'], token, counts['error'], counts['warning'], counts['info'], schema_issues,
                policy['hypothesis_label'], findings[0]['entity_ref'] if findings else None, action, body, utc_now(),
                policy['generator_ref'], 1, json_text({'source_status': status})))
    for row in evidence:
        db.execute('INSERT OR IGNORE INTO summary_evidence_refs VALUES(?,?,?,?,?,?,?)',
                   (stable_id(run_id, row['evidence_id']), run_id, None, row['evidence_id'], None, 'summary_text', 'observed'))
    for row in findings:
        db.execute('INSERT OR IGNORE INTO summary_evidence_refs VALUES(?,?,?,?,?,?,?)',
                   (stable_id(run_id, row['finding_id']), run_id, None, None, row['finding_id'], 'counts', 'observed'))


def summarize_findings(db, fingerprints: set[str]) -> None:
    for fingerprint in sorted(fingerprints):
        row = db.execute('''SELECT f.*,r.project_id,SUM(o.count) AS total FROM findings f
                            JOIN finding_occurrences o USING(finding_id) JOIN runs r ON r.run_id=f.run_id
                            WHERE f.finding_fingerprint=? GROUP BY f.finding_id''', (fingerprint,)).fetchone()
        summary_id = stable_id(fingerprint, 'error-summary')
        db.execute('''INSERT INTO error_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(finding_fingerprint) DO UPDATE SET lifecycle_status=excluded.lifecycle_status,
                      first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,
                      total_occurrences=excluded.total_occurrences''',
                   (summary_id, fingerprint, row['diagnostic_code'], row['diagnostic_code'], None, None, row['status'],
                    row['first_seen_at'], row['last_seen_at'], row['total'], json_text([row['project_id']])))
        db.execute('INSERT OR IGNORE INTO summary_evidence_refs VALUES(?,?,?,?,?,?,?)',
                   (stable_id(summary_id, row['finding_id']), None, summary_id, None, row['finding_id'],
                    'total_occurrences', 'observed'))
