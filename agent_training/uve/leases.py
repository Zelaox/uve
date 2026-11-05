from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from agent_training.content_hash import content_hash
from agent_training.manifest_loader import load_json_ref, pipeline_root

# Kernel locks cover the operation even when it exceeds the diagnostic TTL.
_held: dict[str, tuple[BinaryIO, Path]] = {}
_mutex = threading.Lock()


def _lock(stream: BinaryIO, unlock: bool = False) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


class LeaseManager:
    def __init__(self, ref: str = "config/uve/uve_resource_lock_manifest.json") -> None:
        self._manifest = load_json_ref(ref)
        self._dir = (pipeline_root() / self._manifest["lock_store_relative"]).resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, resource_kind: str, key: str) -> Path:
        digest = content_hash([resource_kind, key]).split(":")[-1]
        return self._dir / f"{digest}.lock"

    def acquire(self, resource_kind: str, key: str, holder: str) -> str | None:
        if resource_kind not in self._manifest["resource_kinds"]:
            raise ValueError(f"unknown resource kind: {resource_kind}")
        path = self._path(resource_kind, key)
        with _mutex:
            if any(p == path for _, p in _held.values()):
                return None
            stream = path.open("a+b")
            if path.stat().st_size == 0:
                stream.write(b" ")
                stream.flush()
            try:
                _lock(stream)
            except OSError:
                stream.close()
                return None
            lease_id = str(uuid.uuid4())
            payload = {"lease_id": lease_id, "resource_kind": resource_kind, "key": key,
                       "holder": holder, "expires_at": time.time() + self._manifest["lease_ttl_seconds"]}
            try:
                path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
            except BaseException:
                _lock(stream, unlock=True)
                stream.close()
                raise
            _held[lease_id] = (stream, path)
            return lease_id

    def owns(self, resource_kind: str, key: str, lease_id: str) -> bool:
        with _mutex:
            return lease_id in _held and _held[lease_id][1] == self._path(resource_kind, key)

    def release(self, resource_kind: str, key: str, lease_id: str) -> None:
        with _mutex:
            item = _held.get(lease_id)
            if item is None or item[1] != self._path(resource_kind, key):
                return
            stream, path = _held.pop(lease_id)
            try:
                path.with_suffix(".json").unlink(missing_ok=True)
            finally:
                _lock(stream, unlock=True)
                stream.close()
