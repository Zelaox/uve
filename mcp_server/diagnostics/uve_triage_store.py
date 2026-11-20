"""Manifest-bound SQLite index. Existing evidence files remain authoritative."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


BOOTSTRAP = 'config/uve_triage_storage_manifest.json'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def sha256(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def stable_id(*values) -> str:
    return sha256(json_text(values).encode('utf-8'))


def within(root: Path, relative: str | Path) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or '..' in relative.parts:
        raise ValueError(f'Path must be relative to its bound root: {relative}')
    candidate = root / relative
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or candidate != resolved:
        raise ValueError(f'Path is linked or escapes its bound root: {relative}')
    return resolved


@dataclass(frozen=True)
class StoreConfig:
    root: Path
    spec: dict

    def path(self, relative: str | Path) -> Path:
        return within(self.root, relative)

    def document(self, relative: str | Path) -> dict:
        value = json.loads(self.path(relative).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Expected a manifest object')
        return value

    def policy(self, key: str) -> dict:
        return self.document(self.spec[key])

    @property
    def pipeline(self) -> Path:
        return self.path(self.policy('lab_binding_ref')['training_pipeline_relative_from_repo'])

    @property
    def database(self) -> Path:
        return self.path(self.spec['database_relative_from_lab'])


def load_config(lab_root: Path | None = None) -> StoreConfig:
    root = (lab_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    base = json.loads(within(root, BOOTSTRAP).read_text(encoding='utf-8'))
    override = os.environ.get(base['db_manifest_override_env'])
    if override:
        candidate = Path(override)
        if candidate.is_absolute():
            candidate = candidate.relative_to(root)
        base = json.loads(within(root, candidate).read_text(encoding='utf-8'))
    if base.get('schema_version') != 'uve_triage_storage.v1':
        raise ValueError('Unsupported observability storage manifest')
    return StoreConfig(root, base)


def _execute_sql(db: sqlite3.Connection, source: str) -> None:
    pending = ''
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            db.execute(pending)
            pending = ''
    if pending.strip():
        raise ValueError('Incomplete SQL migration')


@contextmanager
def transaction(db: sqlite3.Connection):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate(db: sqlite3.Connection, config: StoreConfig) -> None:
    schema = config.path(config.spec['schema_sql_ref'])
    directory = config.path(config.spec['migration_dir_ref'])
    paths = [schema, *sorted(directory.glob(config.spec['migration_glob']))]
    with transaction(db):
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, '
                   'content_sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)')
        applied = dict(db.execute('SELECT migration_id,content_sha256 FROM schema_migrations'))
        for path in paths:
            raw = config.path(path.relative_to(config.root)).read_bytes()
            migration_id = path.relative_to(config.root).as_posix()
            digest = sha256(raw)
            if migration_id in applied:
                if applied[migration_id] != digest:
                    raise ValueError(f'Applied migration was modified: {migration_id}')
                continue
            _execute_sql(db, raw.decode('utf-8'))
            db.execute('INSERT INTO schema_migrations VALUES (?,?,?)', (migration_id, digest, utc_now()))
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Migration produced invalid foreign keys')


@contextmanager
def open_store(config: StoreConfig | None = None, *, readonly: bool = False):
    config = config or load_config()
    path = config.database
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    timeout = config.spec['busy_timeout_ms']
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('Invalid SQLite busy timeout')
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rwc'), uri=True,
                         timeout=timeout / 1000, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if readonly:
            db.execute('PRAGMA query_only=ON')
        else:
            if config.spec['wal_mode'] and db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise ValueError('SQLite did not enable WAL')
            migrate(db, config)
        yield db
    finally:
        db.close()
