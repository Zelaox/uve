"""Inventory the configured source corpus and optionally reconcile it with the index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.uve_triage_sources import discover, raw_source, source_project, sqlite_rows
from mcp_server.diagnostics.uve_triage_store import load_config, open_store, sha256, transaction, utc_now
from mcp_server.diagnostics.uve_triage_migration import cutover_status, record_reconciliation


def inventory(config, *, reconcile=False):
    spec = config.policy('ingest_ref')
    sources = []
    proposals = set()
    errors = []
    excluded = []
    for source in discover(config, spec):
        try:
            raw = raw_source(source, spec['max_source_bytes'])
            entry = {'source_type': source.route['source_type'], 'root_kind': source.route['root_kind'],
                     'relative': source.relative, 'sha256': sha256(raw), 'bytes': len(raw)}
            if source.route['normalizer'] not in ('jsonl', 'advisor_jsonl', 'advisor_sqlite'):
                if source_project(config, spec, source, json.loads(raw)) is None:
                    excluded.append(source.relative)
                    continue
            if source.route['normalizer'] == 'advisor_sqlite':
                proposals.update(row['proposal_id'] for row in sqlite_rows(source, spec['max_records_per_source'])['proposals'])
            sources.append(entry)
        except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError, KeyError) as error:
            errors.append({'relative': source.relative, 'error_class': type(error).__name__})
    counts = {key: sum(row['source_type'] == key for row in sources) for key in
              {route['source_type'] for route in spec['routes']}}
    report = {'observed_at': utc_now(), 'counts': counts, 'sources': sources, 'errors': errors,
              'excluded_count': len(excluded), 'legacy_proposal_count': len(proposals)}
    if reconcile:
        with open_store(config, readonly=True) as db:
            pointers = set(map(tuple, db.execute('''SELECT e.root_kind,e.uri_relative,e.content_sha256 FROM evidence_refs e
                                                     JOIN indexed_sources USING(evidence_id)''')))
            indexed_proposals = {row[0] for row in db.execute('SELECT proposal_id FROM proposals')}
            missing = [row for row in sources if (row['root_kind'], row['relative'], row['sha256']) not in pointers]
            report['reconciliation'] = {'unmatched_sources': missing, 'missing_proposal_ids': sorted(proposals - indexed_proposals),
                                        'integrity_check': db.execute('PRAGMA integrity_check').fetchone()[0],
                                        'run_count': db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
                                        'summary_count': db.execute('SELECT COUNT(*) FROM run_summaries').fetchone()[0],
                                        'comparison_count': db.execute('SELECT COUNT(*) FROM run_comparisons').fetchone()[0],
                                        'proposal_link_count': db.execute('SELECT COUNT(*) FROM finding_proposals').fetchone()[0],
                                        'checked_gate_link_count': db.execute('SELECT COUNT(*) FROM gate_links WHERE checked=1').fetchone()[0]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-report', action='store_true')
    parser.add_argument('--reconcile', action='store_true')
    parser.add_argument('--record-reconciliation', action='store_true')
    parser.add_argument('--assess-cutover', action='store_true')
    args = parser.parse_args()
    config = load_config(ROOT)
    if args.assess_cutover:
        with open_store(config, readonly=True) as db:
            print(json.dumps(cutover_status(db, config)))
        return 0
    if args.record_reconciliation and not args.reconcile:
        parser.error('--record-reconciliation requires --reconcile')
    report = inventory(config, reconcile=args.reconcile)
    if args.record_reconciliation:
        with open_store(config) as db, transaction(db):
            report['reconciliation_id'] = record_reconciliation(db, report)
    if args.write_report:
        location = config.document(config.policy('ingest_ref')['inventory_output_ref'])['relative_path']
        path = config.path(location) / f'uve_triage_migration_inventory_{uuid4().hex}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in report.items() if key != 'sources'} | {'report_path': str(path)}))
    else:
        print(json.dumps(report, indent=2))
    reconciliation = report.get('reconciliation', {})
    invalid = args.reconcile and (reconciliation['integrity_check'] != 'ok'
                                  or reconciliation['summary_count'] != reconciliation['run_count'])
    return int(bool(report['errors'] or invalid or reconciliation.get('missing_proposal_ids') or reconciliation.get('unmatched_sources')))


if __name__ == '__main__':
    raise SystemExit(main())
