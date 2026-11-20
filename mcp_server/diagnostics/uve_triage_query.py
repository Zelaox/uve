"""Read the observability index or persist an explicit run comparison."""
from __future__ import annotations

import argparse
import json

from .uve_triage_compare import compare_runs, read_comparison
from .uve_triage_proof import evidence_document
from .uve_triage_store import load_config, open_store, transaction


def _one(db, sql, value):
    row = db.execute(sql, (value,)).fetchone()
    if row is None:
        raise ValueError('No indexed record matches the requested ID')
    return dict(row)


def _rows(db, sql, *values):
    return [dict(row) for row in db.execute(sql, values)]


def _receipt_explanations(db, config, evidence):
    results = []
    for evidence_id in dict.fromkeys(row["evidence_id"] for row in evidence):
        try:
            _, receipt = evidence_document(db, config, evidence_id)
        except (OSError, ValueError):
            results.append({"evidence_id": evidence_id, "status": "evidence_unavailable"})
            continue
        explanation = receipt.get("explanation")
        results.append({
            "evidence_id": evidence_id,
            "probe_run_id": receipt.get("probe_run_id"),
            "trust_chain_ref": receipt.get("trust_chain_ref"),
            "status": "recorded" if isinstance(explanation, dict) else "legacy_receipt",
            "explanation": explanation if isinstance(explanation, dict) else None,
        })
    return results


def read_query(config, command, value, *, limit=None):
    cap = config.policy('adapter_ref')['max_query_rows']
    if limit is None:
        limit = cap
    if type(limit) is not int or not 1 <= limit <= cap:
        raise ValueError('Query limit is outside the configured budget')
    with open_store(config, readonly=True) as db:
        db.execute('BEGIN')
        if command == 'comparison-show':
            return read_comparison(db, value)
        if command == 'project-health':
            return {'runs': _rows(db, '''SELECT s.*,r.run_kind,r.started_at FROM run_summaries s JOIN runs r USING(run_id)
                                          WHERE r.project_id=? ORDER BY r.started_at DESC,r.run_id LIMIT ?''', value, limit)}
        if command == 'run-summary':
            summary = _one(db, '''SELECT s.*,r.run_kind FROM run_summaries s
                                  JOIN runs r USING(run_id) WHERE r.run_id=?''', value)
            evidence = _rows(db, '''SELECT se.field_name,se.certainty,e.* FROM summary_evidence_refs se
                                    JOIN evidence_refs e USING(evidence_id) WHERE se.run_id=? LIMIT ?''', value, limit)
            result = {
                'summary': summary,
                'findings': _rows(db, '''SELECT s.*,SUM(o.count) AS occurrences_in_run FROM finding_occurrences o
                                         JOIN findings f USING(finding_id) JOIN error_summaries s USING(finding_fingerprint)
                                         WHERE o.run_id=? GROUP BY s.error_summary_id ORDER BY occurrences_in_run DESC LIMIT ?''',
                                  value, limit),
                'evidence': evidence,
            }
            if summary['run_kind'] in ('uve_probe', 'uve_gate'):
                result['receipt_explanations'] = _receipt_explanations(db, config, evidence)
            return result
        if command not in ('fingerprint-timeline', 'trace-finding'):
            raise ValueError('Unknown read operation')
        finding = _one(db, 'SELECT * FROM findings WHERE finding_fingerprint=?', value)
        result = {
            'finding': finding,
            'occurrences': _rows(db, '''SELECT o.*,r.started_at,r.run_kind,r.correlation_json FROM finding_occurrences o
                                        JOIN runs r USING(run_id) WHERE o.finding_id=?
                                        ORDER BY r.started_at,o.occurrence_id LIMIT ?''', finding['finding_id'], limit),
            'comparisons': _rows(db, '''SELECT i.*,c.run_id_a,c.run_id_b,c.compared_at FROM run_comparison_items i
                                        JOIN run_comparisons c USING(comparison_id)
                                        WHERE i.finding_fingerprint=? ORDER BY c.compared_at,i.item_id LIMIT ?''', value, limit),
            'proposals': _rows(db, '''SELECT p.* FROM finding_proposals fp JOIN proposals p USING(proposal_id)
                                      WHERE fp.finding_fingerprint=? ORDER BY p.updated_at,p.proposal_id LIMIT ?''', value, limit),
            'resolution_receipts': _rows(db, '''SELECT rl.*,e.uri_relative,e.content_sha256 FROM resolution_links rl
                                                 JOIN evidence_refs e ON e.evidence_id=rl.gate_receipt_ref
                                                 WHERE rl.finding_fingerprint=? ORDER BY rl.verified_at LIMIT ?''', value, limit),
        }
        if command == 'trace-finding':
            result['proposal_evaluations'] = _rows(db, '''SELECT e.* FROM finding_proposals fp
                JOIN proposal_evaluations e USING(proposal_id)
                WHERE fp.finding_fingerprint=? ORDER BY e.generation DESC,e.evaluation_id LIMIT ?''',
                finding['finding_fingerprint'], limit)
            result['events'] = _rows(db, '''SELECT DISTINCT e.* FROM finding_occurrences o JOIN events e USING(run_id)
                                           WHERE o.finding_id=? ORDER BY e.observed_at,e.event_id LIMIT ?''',
                                     finding['finding_id'], limit)
            result['evidence'] = _rows(db, '''SELECT DISTINCT e.* FROM finding_evidence fe JOIN evidence_refs e USING(evidence_id)
                                             WHERE fe.finding_id=? ORDER BY e.uri_relative LIMIT ?''', finding['finding_id'], limit)
            result['run_links'] = _rows(db, '''SELECT DISTINCT rl.* FROM finding_occurrences o
                                              JOIN run_links rl ON o.run_id=rl.from_run_id OR o.run_id=rl.to_run_id
                                              WHERE o.finding_id=? ORDER BY rl.from_run_id,rl.to_run_id LIMIT ?''',
                                        finding['finding_id'], limit)
            result['linked_evidence'] = _rows(db, '''SELECT DISTINCT re.role,e.* FROM finding_occurrences o
                                                     JOIN run_evidence re USING(run_id)
                                                     JOIN evidence_refs e USING(evidence_id)
                                                     WHERE o.finding_id=? ORDER BY e.uri_relative LIMIT ?''',
                                              finding['finding_id'], limit)
        return result


def mcp_read_query(command: str, identifier: str, limit: int | None = None) -> dict:
    """Bounded handler for the Lab read-only tool registry."""
    config = load_config()
    adapter = config.policy('adapter_ref')
    if not adapter['mcp_query_enabled']:
        raise ValueError('Observability queries are disabled by the adapter manifest')
    if command not in adapter['mcp_query_commands']:
        raise ValueError('This query is outside the registered read-only command set')
    return read_query(config, command, identifier, limit=limit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='command', required=True)
    for name, flag in (('run-summary', '--run-id'), ('comparison-show', '--comparison-id'),
                       ('fingerprint-timeline', '--fp'), ('trace-finding', '--fingerprint'),
                       ('project-health', '--project-id')):
        command = operations.add_parser(name)
        command.add_argument(flag, dest='identifier', required=True)
        command.add_argument('--limit', type=int)
    diff = operations.add_parser('diff-runs')
    diff.add_argument('--a', required=True)
    diff.add_argument('--b', required=True)
    args = parser.parse_args(argv)
    config = load_config()
    try:
        if args.command == 'diff-runs':
            with open_store(config) as db, transaction(db):
                result = compare_runs(db, config, args.a, args.b)
        else:
            result = read_query(config, args.command, args.identifier, limit=args.limit)
    except (ValueError, OSError) as error:
        parser.exit(2, f'{error}\n')
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
