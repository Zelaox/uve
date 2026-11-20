"""Resolve source routes and project only indexable metadata from their files."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .uve_triage_store import sha256, stable_id, within


@dataclass(frozen=True)
class Source:
    route: dict
    path: Path
    root: Path

    @property
    def relative(self) -> str:
        return self.path.relative_to(self.root).as_posix()


def excluded(text: str, values: list[str]) -> bool:
    return any(value.casefold() in text.casefold() for value in values)


def pipeline_document(config, relative):
    return json.loads(within(config.pipeline, relative).read_text(encoding='utf-8'))


def source_project(config, spec, source, document):
    route = source.route
    subject = document.get('subject_identity') or {}
    project = identifier(subject.get('project_id') or document.get('project_id')) or route['project_id']
    if route['normalizer'] == 'bundle' and (document.get('ailab_engine') or {}).get('binding_ref') == spec['engine_binding_ref']:
        project = config.document(spec['engine_binding_ref'])['project_id']
    if route['normalizer'] == 'gate':
        projects = set()
        for ref in document.get('probe_receipt_refs', []):
            run_id = identifier(ref['probe_run_id'])
            if not run_id:
                raise ValueError('Invalid gate probe identity')
            probe_root = source.path.parent.parent / 'probes'
            receipt_path = within(source.root, (probe_root / (run_id + '.json')).relative_to(source.root))
            if receipt_path.is_file():
                probe = json.loads(raw_source(Source(route, receipt_path, source.root), spec['max_source_bytes']))
                if value := identifier((probe.get('subject_identity') or {}).get('project_id')):
                    projects.add(value)
        if any(excluded(value, spec['excluded_project_ids']) for value in projects):
            return None
        if len(projects) == 1:
            project = projects.pop()
    return None if excluded(project, spec['excluded_project_ids']) else project


def discover(config, spec, source_types=None):
    binding = config.document(spec['engine_binding_ref'])
    bridge = config.document(spec['bridge_ref'])
    bound = {
        'bridge_report': config.document(bridge['report_output_root_ref'])['relative_path'],
        'engine_report': config.document(spec['engine_triage_policy_ref'])['report_dir'],
    }
    if config.pipeline.is_dir():
        for key, ref, field in (
            ('uve_log', 'uve_log_root_ref_in_pipeline', 'log_dir'),
            ('advisor_store', 'advisor_storage_ref_in_pipeline', 'database_relative_from_lab'),
            ('advisor_jsonl', 'advisor_policy_ref_in_pipeline', 'jsonl_relative_from_lab'),
        ):
            if within(config.pipeline, spec[ref]).is_file():
                bound[key] = pipeline_document(config, spec[ref])[field]
    for original in spec['routes']:
        if source_types is not None and original['source_type'] not in source_types:
            continue
        route = dict(original)
        if route.get('project_binding') == 'engine':
            route['project_id'] = binding['project_id']
        root = {'lab': config.root, 'pipeline': config.pipeline}[route['root_kind']]
        key = route.get('root_binding')
        if key and key not in bound:
            continue
        prefix = within(root, bound[key]) if key else root
        paths = sorted(prefix.glob(route['glob'])) if route['glob'] else [prefix]
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if excluded(relative, spec['excluded_path_parts']):
                continue
            try:
                checked = within(root, relative)
            except ValueError:
                continue
            if checked.is_file():
                yield Source(route, checked, root)


def raw_source(source: Source, maximum: int) -> bytes:
    before = source.path.stat()
    with source.path.open('rb') as stream:
        data = stream.read(maximum + 1)
    after = source.path.stat()
    if len(data) > maximum:
        raise ValueError('Source exceeds the configured byte budget')
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('Source changed while reading')
    return data


def evidence(db, config, source: Source, raw: bytes, *, trust=None) -> str:
    digest = sha256(raw)
    root_kind = source.route['root_kind']
    identifier = stable_id(root_kind, source.relative, digest)
    bridge = config.document(config.policy('ingest_ref')['bridge_ref'])
    catalog = config.document(bridge['evidence_trust_catalog_ref'])
    tier = trust or source.route['tier']
    if tier not in catalog['trust_tiers']:
        raise ValueError('Evidence trust tier is not configured')
    db.execute('INSERT OR IGNORE INTO evidence_refs VALUES(?,?,?,?,?,?,?,?)',
               (identifier, root_kind, source.relative, digest, tier, catalog['default_authority_ref'], len(raw),
                'application/vnd.sqlite3' if source.route['normalizer'] == 'advisor_sqlite' else 'application/json'))
    return identifier


def identifier(value, *, maximum=160):
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > maximum or not re.fullmatch(r'[\w.:-]+', value):
        return None
    return value


def timestamp(value, fallback: str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return fallback


def jsonl(raw: bytes, limit: int) -> list[dict]:
    lines = [line for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
    if len(lines) > limit:
        raise ValueError('JSONL exceeds the configured record budget')
    return [json.loads(line) for line in lines]


def sqlite_rows(source: Source, limit: int) -> dict[str, list[dict]]:
    # A read-only transaction captures the same SQLite snapshot across every table, including WAL data.
    db = sqlite3.connect(source.path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        output = {}
        for name in ('proposals', 'proposal_evaluations', 'fitness_records', 'generation_summaries', 'evidence_holds'):
            if name not in names:
                continue
            rows = [dict(row) for row in db.execute(f'SELECT * FROM {name} LIMIT ?', (limit + 1,))]
            if len(rows) > limit:
                raise ValueError('Advisor snapshot exceeds the configured record budget')
            output[name] = rows
        if 'proposals' not in output:
            raise ValueError('Advisor database lacks its proposal table')
        return output
    finally:
        db.close()
