"""Structural hashing for Lab ↔ UVE bridge (no domain literals)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def structural_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_content_sha256(path) -> str:
    from pathlib import Path

    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest()
