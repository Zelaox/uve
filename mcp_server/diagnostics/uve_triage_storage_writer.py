"""Keep a bounded scratch writer's lease alive through the whole write."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import logging
import os
import sqlite3
from pathlib import Path
from threading import Event, Thread

from .uve_triage_storage_lease import lease, under
from .uve_triage_store import load_config


LOGGER = logging.getLogger(__name__)


@contextmanager
def storage_work(config, work_root, writer_kind, *, linked_run=None):
    from .uve_triage_storage_lifecycle import policies

    policy, _ = policies(config)
    root = Path(work_root).absolute()
    if not root.is_relative_to(config.root):
        yield None
        return
    relative = root.relative_to(config.root).as_posix()
    active = policy['active_work']
    roots = ([row['relative'] for row in policy['scope']['roots']] if active['writer_scope_kinds'][writer_kind] == 'cleanup'
             else active['lease_root_prefixes'])
    if not any(under(relative, row) for row in roots):
        yield None
        return
    actor = active['writer_actor_refs'][writer_kind]
    row = lease(config, policy, 'acquire', work_root=relative, actor=actor, pid=os.getpid(), linked_run=linked_run)
    lease_id = row['lease_id']
    stop = Event()

    def heartbeat():
        while not stop.wait(active['heartbeat_interval_seconds']):
            try:
                lease(config, policy, 'heartbeat', lease_id=lease_id)
            except sqlite3.OperationalError as error:
                if getattr(error, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    # The owner still holds the DB writer lock. Retry after it commits.
                    continue
                LOGGER.exception('Storage heartbeat could not be refreshed for %s', lease_id)
                return
            except Exception:
                LOGGER.exception('Storage heartbeat could not be refreshed for %s', lease_id)
                return

    worker = Thread(target=heartbeat, name='uve-storage-heartbeat', daemon=True)
    worker.start()
    try:
        yield lease_id
    finally:
        stop.set()
        worker.join(timeout=config.spec['busy_timeout_ms'] / 1000 + 1)
        try:
            lease(config, policy, 'release', lease_id=lease_id)
        except Exception:
            LOGGER.exception('Storage lease release could not be recorded for %s', lease_id)


def lease_codegen_writes(function):
    @wraps(function)
    def guarded(**kwargs):
        config = load_config()
        state = kwargs.get('state')
        with storage_work(config, kwargs['project_root'], 'codegen', linked_run=getattr(state, 'run_id', None)):
            return function(**kwargs)

    return guarded
