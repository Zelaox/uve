"""Analyze only approved Lab roots and audit each bounded inventory."""
from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
import hashlib
import os
from pathlib import Path
import stat
import json
from uuid import uuid4

from .uve_triage_sources import excluded
from .uve_triage_storage_lease import protected_leases, under
from .uve_triage_store import json_text, sha256, stable_id


def file_identity(path, chunk_size):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Linked or non-regular file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        handle = os.fstat(stream.fileno())
        if (handle.st_dev, handle.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('File identity changed while opening')
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink):
        raise ValueError('File changed during hash')
    return before, 'sha256:' + digest.hexdigest()


def _manifest_references(config, policy):
    scope = policy['scope']
    excluded_file = config.path(policy['manifest']['scope_ref'])
    references = set()
    seen = 0

    def visit(document, key, root):
        if isinstance(document, dict):
            for child_key, value in document.items():
                visit(value, child_key, root)
        elif isinstance(document, list):
            for value in document:
                visit(value, key, root)
        elif isinstance(document, str) and key.endswith(tuple(scope['reference_field_suffixes'])):
            base = config.root if key.endswith('_relative_from_lab') else root
            value = Path(document)
            try:
                if value.is_absolute():
                    value = value.relative_to(base)
                if '..' in value.parts or any(mark in document for mark in '*?['):
                    return
                resolved = config.path((base / value).relative_to(config.root))
            except ValueError:
                return
            references.add(resolved.relative_to(config.root).as_posix().casefold())

    for route in scope['reference_manifest_routes']:
        root = {'lab': config.root, 'pipeline': config.pipeline}[route['root_kind']]
        if not root.is_dir():
            continue
        for path in sorted(root.glob(route['glob'])):
            checked = config.path(path.relative_to(config.root))
            if checked == excluded_file:
                continue
            seen += 1
            if seen > policy['manifest']['scan_limit'] or checked.stat().st_size > scope['reference_manifest_max_bytes']:
                raise ValueError('Reference manifest budget exceeded')
            visit(json.loads(checked.read_text(encoding='utf-8-sig')), '', root)
    return references


def _references(db, config=None, policy=None):
    refs = {}
    for row in db.execute("SELECT uri_relative,COUNT(*) AS n FROM evidence_refs WHERE root_kind='lab' GROUP BY uri_relative"):
        refs[row['uri_relative'].casefold()] = row['n']
    if config is not None:
        for reference in _manifest_references(config, policy):
            refs[reference] = refs.get(reference, 0) + 1
    holds = {}
    for row in db.execute('''SELECT h.hold_id,COALESCE(e.uri_relative,h.uri_relative) AS uri,
                             COALESCE(e.root_kind,h.root_kind) AS root
                             FROM evidence_holds h LEFT JOIN evidence_refs e USING(evidence_id)
                             WHERE h.released_at IS NULL'''):
        if row['root'] not in ('lab', 'pipeline') or not row['uri']:
            raise ValueError('Unresolved active evidence hold')
        if row['root'] == 'lab':
            holds.setdefault(row['uri'].casefold(), []).append(row['hold_id'])
    return refs, holds


def classify(config, policy, relative, route):
    scope = policy['scope']
    if excluded(relative, scope['excluded_path_parts']):
        return 'protect', 'excluded_scope'
    permanent = scope['permanent_root_prefixes'] + policy['protected_source_roots']['roots_relative']
    file = Path(relative)
    if (any(under(relative, root) for root in permanent)
            or file.name.casefold() in scope['permanent_names']
            or file.name.casefold().endswith(tuple(scope['permanent_suffixes']))):
        return 'protect', 'permanent_class'
    if not any(fnmatchcase(file.name.casefold(), pattern) for pattern in route['file_globs']):
        raise ValueError('File has no configured lifecycle data class')
    return policy['policy']['age_decisions'][route['data_class_ref']], 'retention'


def analyze(db, config, policy, now):
    manifest, scope = policy['manifest'], policy['scope']
    refs, holds = _references(db, config, policy)
    leases = [row['work_root_uri_relative'] for row in protected_leases(db, policy, now)]
    observed = 0
    for route in scope['roots']:
        directory = config.path(route['relative'])
        if excluded(route['relative'], scope['excluded_path_parts']):
            raise ValueError('Configured scan root matches a scope exclusion')
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError('Configured scan root is not a directory')
        for parent, subdirs, files in os.walk(directory, followlinks=False, onerror=lambda error: _raise(error)):
            for subdir in subdirs[:]:
                relative = (Path(parent) / subdir).relative_to(config.root).as_posix()
                if excluded(relative, scope['excluded_path_parts']):
                    subdirs.remove(subdir)
                    continue
                config.path(relative)
            for name in sorted(files):
                observed += 1
                if observed > manifest['scan_limit']:
                    raise ValueError('Configured inventory file budget exceeded')
                relative = (Path(parent) / name).relative_to(config.root).as_posix()
                if excluded(relative, scope['excluded_path_parts']):
                    continue
                path = config.path(relative)
                current = path.lstat()
                age = max(0, int(now.timestamp() - current.st_mtime))
                decision, reason = classify(config, policy, relative, route)
                references = sum(count for uri, count in refs.items() if under(relative, uri))
                hold_ids = sorted(hold for uri, ids in holds.items() if under(relative, uri) for hold in ids)
                digest = None
                if any(under(relative, root) for root in leases):
                    decision, reason = 'protect', 'active_work_lease'
                elif references or hold_ids:
                    decision, reason = 'protect', 'reference_or_hold'
                elif decision != 'protect':
                    minimum = policy['policy']['durations_days'][route['data_class_ref']]
                    if age < minimum * 86400:
                        decision, reason = 'keep', 'retention_period'
                    elif decision in ('quarantine', 'archive'):
                        current, digest = file_identity(path, manifest['hash_chunk_bytes'])
                yield {'uri_relative': relative, 'root_kind': 'lab', 'data_class_ref': route['data_class_ref'],
                       'age_seconds': age, 'byte_size': current.st_size, 'content_sha256': digest,
                       'reference_count': references, 'hold_ids_json': json_text(hold_ids),
                       'decision': decision, 'decision_reason_ref': reason}


def _raise(error):
    raise error


def audit_scan(db, config, policy, digest, actor, now, *, reconciliation_kind=None):
    run_id = str(uuid4())
    stamp = now.isoformat()
    error = None
    try:
        candidates = sorted(analyze(db, config, policy, now), key=lambda item: item['uri_relative'])
    except (OSError, ValueError, KeyError) as failure:
        candidates = []
        error = type(failure).__name__
    plan = stable_id(digest, run_id, candidates)
    protected = sum(row['decision'] == 'protect' for row in candidates)
    reclaim = sum(row['byte_size'] for row in candidates if row['decision'] == 'quarantine')
    scanned = sum(row['byte_size'] for row in candidates)
    quarantine = config.path(policy['manifest']['quarantine_relative'])
    held_bytes = sum(path.stat().st_size for path in quarantine.iterdir()
                     if not path.is_symlink() and path.is_file()) if quarantine.is_dir() else 0
    result = {'cleanup_run_id': run_id, 'mode': policy['manifest']['default_mode'], 'manifest_sha256': digest,
              'plan_sha256': plan, 'bytes_scanned': scanned, 'bytes_reclaimable': reclaim,
              'protected_count': protected, 'quarantine_bytes': held_bytes, 'aborted': bool(error), 'abort_reason': error}
    db.execute('''INSERT INTO cleanup_runs(cleanup_run_id,mode,actor_class_ref,started_at,finished_at,manifest_sha256,
                  bytes_scanned,bytes_reclaimable,protected_count,aborted,abort_reason,reconciliation_kind,
                  plan_sha256,storage_health_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (run_id, result['mode'], actor, stamp, stamp, digest, scanned, reclaim,
                protected, int(bool(error)), error, reconciliation_kind, plan, json_text(result)))
    for row in candidates:
        db.execute('INSERT INTO cleanup_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (stable_id(run_id, row['uri_relative']), run_id, *row.values()))
    db.execute('INSERT INTO storage_health_snapshots VALUES(?,?,?,?,?,?,?,?)',
               (stable_id(run_id, 'health'), run_id, stamp, scanned, reclaim, protected, held_bytes, 'measured_configured_roots'))
    return result
