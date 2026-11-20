"""Index manifest-selected triage sources without changing the originals."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .uve_triage_compare import compare_runs
from .uve_triage_proof import link_indexed_gates
from .uve_triage_sources import Source, discover, evidence, excluded, identifier, jsonl, raw_source, source_project, sqlite_rows, timestamp
from .uve_triage_store import json_text, load_config, open_store, sha256, stable_id, transaction, utc_now
from .uve_triage_summarize import summarize_findings, summarize_run
from .uve_triage_storage_activity import record_activity
from .uve_triage_storage_writer import storage_work


def _records(document):
    for name in ('findings', 'lab_findings', 'schema_issues', 'items'):
        value = document.get(name)
        if isinstance(value, list):
            yield from value
    for name in ('runtime_triage', 'uve_triage_projection'):
        value = document.get(name)
        if isinstance(value, dict):
            yield from _records(value)
    for code in document.get('error_classes') or []:
        if identifier(code):
            yield {'code': code, 'severity': 'error'}


def engine_diagnostic_codes(config, spec):
    rules = config.document(spec['engine_triage_policy_ref'])
    return {item['diagnostic_code'] for group in spec['engine_diagnostic_rule_groups'] for item in rules[group]} | set(
        spec['engine_diagnostic_additional_codes'])


def _finding(db, config, record, run_id, project, observed, evidence_id, index, epoch):
    if not isinstance(record, dict):
        return None
    code = identifier(record.get('diagnostic_code') or record.get('finding_code') or record.get('code') or record.get('category_id'))
    if code is None:
        return None
    entity = identifier(record.get('entity_ref') or record.get('entity_id') or record.get('entity_key'))
    fields = {'project_id': project, 'diagnostic_code': code, 'entity_ref': entity}
    fingerprint = stable_id([fields[field] for field in config.policy('fingerprint_ref')['default_fields']])
    severity = identifier(record.get('severity') or record.get('severity_id')) or config.policy('summary_ref')['severity_by_code'].get(code, 'info')
    finding_id = stable_id(fingerprint, 'finding')
    count = record.get('count', 1)
    if type(count) is not int or not 0 < count <= 2**31 - 1:
        raise ValueError('Invalid source occurrence count')
    certainty = 'hypothesis' if record.get('certainty') in ('hypothesis', 'inferred') else 'observed'
    db.execute('''INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(finding_fingerprint) DO UPDATE SET
                  first_seen_at=MIN(first_seen_at,excluded.first_seen_at),last_seen_at=MAX(last_seen_at,excluded.last_seen_at)''',
               (finding_id, run_id, code, severity, certainty, fingerprint, observed, observed, 'new', entity, '{}'))
    db.execute('INSERT OR IGNORE INTO finding_occurrences VALUES(?,?,?,?,?,?,?)',
               (stable_id(run_id, index, fingerprint), finding_id, run_id, epoch, evidence_id, count, severity))
    db.execute('INSERT OR IGNORE INTO finding_evidence VALUES(?,?,?)', (finding_id, evidence_id, 'source'))
    return fingerprint


def _advisor(db, config, spec, snapshot, evidence_id, observed, run_id, source_kind):
    observed_count = 0
    fields = spec['advisor_evaluation_projection_fields']
    for row in snapshot.get('proposals', []):
        proposal = identifier(row.get('proposal_id'))
        if not proposal:
            continue
        updated = timestamp(row.get('updated_at') or row.get('created_at'), observed)
        confidence = row.get('gap_confidence')
        payload = json.loads(row['record_json']) if isinstance(row.get('record_json'), str) else row
        observed_count += payload.get('gap_confidence') == 'observed'
        metadata = {key: value for key in ('generation', 'variant_id', 'gap_confidence', 'static_analysis_only')
                    if type(value := payload.get(key)) in (str, int, bool)}
        db.execute('''INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(proposal_id) DO UPDATE SET status=excluded.status,
                      proposal_outcome=excluded.proposal_outcome,gap_confidence=excluded.gap_confidence,
                      updated_at=excluded.updated_at,source_ref=excluded.source_ref,record_json=excluded.record_json
                      WHERE excluded.updated_at >= proposals.updated_at''',
                   (proposal, identifier(row.get('parent_scenario')), identifier(row.get('strategy_id')),
                    identifier(row.get('status')), identifier(row.get('proposal_outcome')),
                    confidence if isinstance(confidence, (int, float)) else None, json_text(metadata), updated, evidence_id))
        state = {key: value for key, value in payload.items() if key not in ('created_at', 'updated_at')}
        db.execute('INSERT OR IGNORE INTO proposal_history VALUES(?,?,?,?,?,?,?)',
                   (stable_id(proposal, 'indexed', state), proposal, 'indexed', None, updated, None, '{}'))
        for fingerprint in payload.get('finding_fingerprints', []):
            if isinstance(fingerprint, str):
                db.execute('''INSERT OR IGNORE INTO finding_proposals SELECT finding_fingerprint,? FROM findings
                              WHERE finding_fingerprint=?''', (proposal, fingerprint))
        for relative in payload.get('evidence_refs') or []:
            if not isinstance(relative, str):
                continue
            try:
                path = config.path(relative)
                reference = path.relative_to(config.root).as_posix()
                if not db.execute("SELECT 1 FROM evidence_refs WHERE root_kind='lab' AND uri_relative=?", (reference,)).fetchone():
                    continue
                digest = sha256(path.read_bytes()) if path.stat().st_size <= spec['max_source_bytes'] else None
            except (ValueError, OSError):
                continue
            db.execute('''INSERT OR IGNORE INTO finding_proposals
                          SELECT DISTINCT f.finding_fingerprint,? FROM evidence_refs e JOIN run_evidence re USING(evidence_id)
                          JOIN finding_occurrences o USING(run_id) JOIN findings f USING(finding_id)
                          WHERE e.root_kind='lab' AND e.uri_relative=? AND e.content_sha256=?''',
                       (proposal, reference, digest))
    for row in snapshot.get('proposal_evaluations', []):
        payload = json.loads(row['record_json'])
        metadata = {key: payload[key] for key in fields if key in payload}
        db.execute('INSERT OR IGNORE INTO proposal_evaluations VALUES(?,?,?,?,?,?)',
                   (row['evaluation_id'], row['proposal_id'], row.get('generation'), identifier(payload.get('variant_id')),
                    identifier(row.get('inputs_hash')), json_text(metadata)))
        probe_id = identifier(payload.get('probe_run_id'))
        linked = db.execute('SELECT 1 FROM probe_results WHERE probe_run_id=?', (probe_id,)).fetchone()
        db.execute('INSERT OR IGNORE INTO proposal_history VALUES(?,?,?,?,?,?,?)',
                   (stable_id(row['evaluation_id'], 'evaluated'), row['proposal_id'], 'evaluated', None,
                    observed, probe_id if linked else None, json_text(metadata)))
    for row in snapshot.get('fitness_records', []):
        metrics = {key: row[key] for key in ('defect_discovery_fitness', 'preventive_coverage_value') if key in row}
        if (evaluation := identifier(json.loads(row['record_json']).get('evaluation_id'))):
            metrics['evaluation_id'] = evaluation
        proposal = row.get('proposal_id')
        if proposal and not db.execute('SELECT 1 FROM proposals WHERE proposal_id=?', (proposal,)).fetchone():
            metrics['legacy_proposal_id'] = proposal
            proposal = None
        db.execute('INSERT OR IGNORE INTO fitness_records VALUES(?,?,?,?,?)',
                   (row['fitness_id'], row.get('strategy_id'), proposal, row.get('generation'), json_text(metrics)))
    for row in snapshot.get('generation_summaries', []):
        source = json.loads(row.get('summary_json') or '{}')
        metrics = {key: source[key] for key in spec['advisor_generation_summary_fields'] if key in source}
        db.execute('INSERT OR REPLACE INTO evolution_generations VALUES(?,?)', (row['generation'], json_text(metrics)))
    for row in snapshot.get('evidence_holds', []):
        # The source pointer conservatively holds the advisor snapshot when the legacy ID cannot resolve a file.
        db.execute('INSERT OR IGNORE INTO evidence_holds VALUES(?,?,?,?,?,?)',
                   (stable_id('advisor', row['hold_id']), evidence_id, None, None, 'advisor_legacy_hold', None))
    generation = max((row['generation'] for row in snapshot.get('generation_summaries', [])), default=None)
    summary = db.execute('SELECT summary_json FROM evolution_generations WHERE generation=?', (generation,)).fetchone()
    db.execute('INSERT OR IGNORE INTO advisor_snapshots VALUES(?,?,?,?,?,?,?,?)',
               (run_id, source_kind, utc_now(), len(snapshot.get('proposals', [])), observed_count,
                len(snapshot.get('fitness_records', [])), generation, summary[0] if summary else None))


def _index(db, config, spec, source, batch):
    raw = raw_source(source, spec['max_source_bytes'])
    evidence_id = evidence(db, config, source, raw)
    route = source.route
    normalizer = route['normalizer']
    source_type = route['source_type']
    revision = spec['projection_revision']
    previous = db.execute('SELECT projection_revision FROM indexed_sources WHERE source_type=? AND evidence_id=?',
                          (source_type, evidence_id)).fetchone()
    if normalizer != 'advisor_sqlite' and previous and previous[0] == revision:
        return None, set(), 'unchanged'
    limit = spec['max_records_per_source']
    snapshot = None
    if normalizer == 'advisor_sqlite':
        snapshot = sqlite_rows(source, limit)
        document = {}
    elif normalizer in ('advisor_jsonl', 'jsonl'):
        entries = jsonl(raw, limit)
        snapshot = {'proposals': entries} if normalizer == 'advisor_jsonl' else None
        document = {'items': entries} if normalizer == 'jsonl' else {}
    else:
        document = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(document, dict):
        raise ValueError('Source document must be an object')
    subject = document.get('subject_identity') or {}
    project = source_project(config, spec, source, document)
    if project is None:
        return None, set(), 'excluded'
    kind = route['run_kind']
    if kind not in config.policy('run_kind_catalog_ref')['kinds']:
        raise ValueError('Source uses an unregistered run kind')
    observed = timestamp(document.get('started_at') or document.get('generated_at') or document.get('created_at')
                         or document.get('observed_at'), datetime.fromtimestamp(source.path.stat().st_mtime, timezone.utc).isoformat())
    correlation = {key: value for key in ('logical_incident_id', 'occurrence_id', 'probe_run_id', 'proposal_id', 'run_id')
                   if (value := identifier(document.get(key)))}
    probe_link = identifier((document.get('correlation') or {}).get('probe_run_id'))
    if probe_link:
        correlation['probe_run_id'] = probe_link
    epoch = identifier(document.get('evidence_epoch')) or evidence_id
    correlation['evidence_epoch'] = epoch
    if snapshot is not None:
        correlation['snapshot_sha256'] = stable_id(snapshot)
    run_id = stable_id(source_type, evidence_id, snapshot) if snapshot is not None else stable_id(source_type, evidence_id)
    status = identifier(document.get('status') or document.get('verdict') or document.get('outcome')) or 'observed'
    if document.get('blocking') is True or document.get('schema_blocking_issue_count', 0):
        status = 'failed'
    if normalizer in ('gate', 'probe') and status not in ('failed', 'blocked', 'error'):
        status = 'not_verified'
    db.execute('INSERT OR IGNORE INTO projects(project_id,binding_manifest_ref) VALUES(?,?)',
               (project, spec['engine_binding_ref'] if route.get('project_binding') else spec['bridge_ref']))
    is_new = db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,NULL)',
                        (run_id, project, kind, observed, observed, status, json_text(correlation))).rowcount
    if not is_new and previous and previous[0] == revision:
        return None, set(), 'unchanged'
    if not is_new:
        db.execute('UPDATE runs SET status=?,correlation_json=? WHERE run_id=?', (status, json_text(correlation), run_id))
        if normalizer == 'gate':
            db.execute('UPDATE runs SET project_id=? WHERE run_id=?', (project, run_id))
    db.execute('INSERT OR IGNORE INTO run_evidence VALUES(?,?,?)', (run_id, evidence_id, 'source'))
    if status in spec['hold_failure_statuses']:
        db.execute('INSERT OR IGNORE INTO evidence_holds VALUES(?,?,?,?,?,?)',
                   (stable_id(run_id, 'failed-run-hold'), evidence_id, None, None, 'failed_run_evidence', None))
    if normalizer == 'bundle':
        _bundle_evidence(db, config, spec, source, document, run_id)
    fingerprints = set()
    known_codes = engine_diagnostic_codes(config, spec) if route.get('enforce_engine_diagnostic_catalog') else None
    for index, record in enumerate(_records(document)):
        if index >= limit:
            raise ValueError('Source exceeds the configured finding budget')
        if known_codes is not None and isinstance(record, dict) and (code := record.get('diagnostic_code')):
            if code not in known_codes and not code.startswith(tuple(spec['engine_diagnostic_prefixes'])):
                raise ValueError('Engine diagnostic code has no configured mapping')
        fingerprint = _finding(db, config, record, run_id, project, observed, evidence_id, index, epoch)
        if fingerprint:
            fingerprints.add(fingerprint)
        if isinstance(record, dict) and (event := identifier(record.get('event_name') or record.get('event')
                                                               or record.get('category_id') or record.get('diagnostic_code'))):
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)',
                       (stable_id(run_id, index), run_id, observed, identifier(record.get('subsystem')), event,
                        identifier(record.get('entity_ref') or record.get('entity_id')), '{}', evidence_id, sha256(raw)))
    if snapshot is not None:
        _advisor(db, config, spec, snapshot, evidence_id, observed, run_id, normalizer)
    if normalizer == 'probe':
        probe = identifier((document.get('probe') or {}).get('probe_id'))
        probe_run = identifier(document.get('probe_run_id'))
        if not probe or not probe_run:
            raise ValueError('Probe receipt lacks its probe identity')
        profile = document.get('profile_ref')
        plan = document.get('probe_plan_ref')
        db.execute('INSERT OR IGNORE INTO probes VALUES(?,?,?)',
                   (probe, profile if isinstance(profile, str) and profile.startswith('config/') else None,
                    identifier(plan.get('manifest_bundle_sha256') if isinstance(plan, dict) else plan)))
        db.execute('INSERT OR IGNORE INTO probe_results VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (probe_run, run_id, probe, status, identifier(document.get('failure_class')), None,
                    evidence_id, identifier(subject.get('identity_sha256')), None,
                    identifier(document.get('verdict')) or 'unknown'))
    db.execute('''INSERT INTO indexed_sources VALUES(?,?,?,?) ON CONFLICT(source_type,evidence_id) DO UPDATE SET
                  projection_revision=excluded.projection_revision''', (source_type, evidence_id, batch, revision))
    return run_id, fingerprints, 'indexed'


def _bundle_evidence(db, config, spec, source, document, run_id):
    bridge = config.document(spec['bridge_ref'])
    trust = config.document(bridge['evidence_trust_catalog_ref'])
    for item in document.get('evidence_trust') or []:
        relative = item.get('ref')
        if (not isinstance(relative, str) or excluded(relative, spec['excluded_path_parts'])
                or item.get('trust_tier') not in ('authoritative', 'derived', 'historical')):
            continue
        try:
            path = config.path(relative)
            allowed = [config.path(root) for root in trust['allowed_read_roots_relative']]
            if not any(path.is_relative_to(root) for root in allowed):
                continue
            attachment = Source(source.route, path, config.root)
            data = raw_source(attachment, spec['max_source_bytes'])
        except (ValueError, OSError):
            continue
        if sha256(data).removeprefix('sha256:') != str(item.get('content_sha256', '')).removeprefix('sha256:'):
            continue
        authority = [config.path(root) for root in trust['authoritative_roots_relative']]
        tier = 'authoritative' if any(path.is_relative_to(root) for root in authority) else 'derived'
        db.execute('INSERT OR IGNORE INTO run_evidence VALUES(?,?,?)',
                   (run_id, evidence(db, config, attachment, data, trust=tier), 'bundle_source'))


def _auto_compare(db, config, runs):
    policy = config.policy('comparison_ref')
    if not policy['auto_compare']:
        return
    for run_id in sorted(runs, key=lambda item: tuple(db.execute(
            'SELECT started_at,run_id FROM runs WHERE run_id=?', (item,)).fetchone())):
        run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        prefix = 'SELECT run_id FROM runs WHERE project_id=?'
        args = [run['project_id']]
        if policy['compare_same_run_kind']:
            prefix += ' AND run_kind=?'
            args.append(run['run_kind'])
        prior = db.execute(prefix + ' AND (started_at,run_id)<(?,?) ORDER BY started_at DESC,run_id DESC LIMIT 1',
                           (*args, run['started_at'], run_id)).fetchone()
        latest = db.execute(prefix + ' ORDER BY started_at DESC,run_id DESC LIMIT 1', args).fetchone()
        if prior:
            compare_runs(db, config, prior[0], run_id, advance=latest[0] == run_id)
        later = db.execute(prefix + ' AND (started_at,run_id)>(?,?) ORDER BY started_at,run_id LIMIT 1',
                           (*args, run['started_at'], run_id)).fetchone()
        if later and later[0] not in runs:
            compare_runs(db, config, run_id, later[0], advance=latest[0] == later[0])


def ingest(config, *, batch_label, actor_class=None, source_types=None, selected_paths=None):
    spec = config.policy('ingest_ref')
    actor = actor_class or spec['default_actor_class']
    if actor not in spec['actor_classes']:
        raise ValueError('Unknown ingest actor class')
    if source_types and set(source_types) - {route['source_type'] for route in spec['routes']}:
        raise ValueError('Unknown source type')
    sources = list(discover(config, spec, source_types))
    if selected_paths is not None:
        requested = {Path(path).resolve() for path in selected_paths}
        sources = [source for source in sources if source.path in requested]
        if {source.path for source in sources} != requested:
            raise ValueError('One or more selected sources fall outside the configured routes')
    started = utc_now()
    batch = str(uuid4())
    findings, runs, errors, gates = set(), [], [], []
    counts = {'indexed': 0, 'unchanged': 0, 'excluded': 0}
    with storage_work(config, config.database.parent, 'indexer', linked_run=batch), open_store(config) as db, transaction(db):
        db.execute('INSERT INTO ingest_batches VALUES(?,?,?,?,?,?,?,?,?)',
                   (batch, 'manifest', actor, started, started, len(sources), 0, 0, '{}'))
        for source in sources:
            db.execute('SAVEPOINT source')
            try:
                run_id, fingerprints, outcome = _index(db, config, spec, source, batch)
                if outcome == 'excluded':
                    db.execute('ROLLBACK TO source')
                if run_id:
                    if source.route['normalizer'] == 'gate':
                        gates.append(db.execute('SELECT evidence_id FROM run_evidence WHERE run_id=?', (run_id,)).fetchone()[0])
                    summarize_run(db, config, run_id)
                    runs.append(run_id)
                    findings.update(fingerprints)
                counts[outcome] += 1
            except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error) as error:
                db.execute('ROLLBACK TO source')
                detail = {'source': source.relative, 'error_class': type(error).__name__}
                if getattr(error, 'sqlite_errorname', None):
                    detail['sqlite_error_name'] = error.sqlite_errorname
                errors.append(detail)
            finally:
                db.execute('RELEASE source')
        for gate_id in gates:
            try:
                link_indexed_gates(db, config, [gate_id])
            except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
                errors.append({'evidence_id': gate_id, 'error_class': type(error).__name__})
        db.execute('''INSERT OR IGNORE INTO run_links SELECT r.run_id,p.run_id,'reported_probe'
                      FROM runs r JOIN probe_results p ON p.probe_run_id=json_extract(r.correlation_json,'$.probe_run_id')
                      WHERE r.run_id!=p.run_id''')
        _auto_compare(db, config, runs)
        summarize_findings(db, findings)
        statuses = spec['hold_failure_statuses']
        markers = ','.join('?' for _ in statuses)
        failed = db.execute(f'''SELECT r.run_id,re.evidence_id FROM runs r JOIN run_evidence re USING(run_id)
                                WHERE re.role='source' AND r.status IN ({markers})''', statuses).fetchall()
        for row in failed:
            db.execute('INSERT OR IGNORE INTO evidence_holds VALUES(?,?,?,?,?,?)',
                       (stable_id(row['run_id'], 'failed-run-hold'), row['evidence_id'], None, None,
                        'failed_run_evidence', None))
        missing = db.execute('''SELECT COUNT(*) FROM runs LEFT JOIN run_summaries USING(run_id)
                                WHERE COALESCE(summary_complete,0)=0''').fetchone()[0]
        inconsistent = db.execute('''SELECT COUNT(*) FROM runs r JOIN run_summaries s USING(run_id)
                                     WHERE r.project_id!=s.project_id''').fetchone()[0]
        integrity = not errors and not missing and not inconsistent and not db.execute('PRAGMA foreign_key_check').fetchone()
        result = {'batch_id': batch, 'batch_label': batch_label, 'integrity_check_ok': bool(integrity),
                  'sources': len(sources), **counts, 'errors': errors, 'missing_summaries': missing,
                  'inconsistent_summary_projects': inconsistent}
        db.execute('UPDATE ingest_batches SET finished_at=?,rows_out=?,integrity_check_ok=?,detail_json=? WHERE batch_id=?',
                   (utc_now(), len(runs), int(integrity), json_text(result), batch))
        if integrity and (runs or actor == 'scheduler_maintenance'):
            from .uve_triage_storage_lifecycle import policies

            if 'storage_lifecycle_ref' in config.spec:
                storage, digest = policies(config)
                record_activity(db, storage, digest, datetime.fromisoformat(utc_now()),
                                storage['activity']['ingest_kind'], actor, batch)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest')
    parser.add_argument('--batch', required=True)
    parser.add_argument('--source-type', action='append')
    parser.add_argument('--source-path', action='append')
    parser.add_argument('--actor-class')
    args = parser.parse_args(argv)
    config = load_config()
    if args.manifest and Path(args.manifest).resolve() != config.path(config.spec['ingest_ref']):
        parser.error('The manifest must match the configured ingest reference')
    result = ingest(config, batch_label=args.batch, source_types=args.source_type, selected_paths=args.source_path,
                    actor_class=args.actor_class)
    print(json_text(result))
    return 0 if result['integrity_check_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
