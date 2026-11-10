from __future__ import annotations

import os
import stat
import hashlib
from pathlib import Path
from typing import Any

from agent_training.content_hash import content_hash_file_text
from agent_training.manifest_loader import ManifestError, load_json_ref, pipeline_root


class EvidenceStore:
    def __init__(self, storage_ref: str = "config/uve/uve_evidence_storage_manifest.json") -> None:
        self._manifest = load_json_ref(storage_ref)
        root = pipeline_root()
        self._root = (root / self._manifest["store_relative"]).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._roles = {r["role_id"]: frozenset(r["capabilities"]) for r in self._manifest["roles"]}

    def _check(self, role_id: str, capability: str) -> None:
        caps = self._roles.get(role_id)
        if caps is None:
            raise ManifestError(f"unknown role: {role_id}")
        if capability not in caps:
            raise ManifestError(f"role {role_id} lacks capability {capability}")

    def write_raw(self, role_id: str, relative_name: str, text: str) -> str:
        self._check(role_id, "write_raw")
        path = self._safe_path(relative_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as stream:
                previous = stream.read()
            if previous != text:
                raise ManifestError("raw evidence is immutable; use a new artifact name")
        else:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
        if self._manifest.get("enforce_os_acl"):
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        return content_hash_file_text(text)

    def read_raw(self, role_id: str, relative_name: str) -> str:
        self._check(role_id, "read_raw")
        path = self._safe_path(relative_name)
        if not path.is_file():
            raise ManifestError(f"artifact not found: {relative_name}")
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()

    def _safe_path(self, relative_name: str) -> Path:
        path = (self._root / relative_name).resolve()
        if not path.is_relative_to(self._root):
            raise ManifestError("evidence path escapes storage root")
        return path

    def write_artifact(self, role_id: str, data: bytes, *, media_type: str) -> dict[str, Any]:
        self._check(role_id, "write_raw")
        digest = hashlib.sha256(data).hexdigest()
        path = self._safe_path(digest)
        if path.exists():
            if path.read_bytes() != data:
                raise ManifestError("content addressed artifact mismatch")
        else:
            try:
                with path.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ManifestError("content addressed artifact mismatch")
        return {"content_sha256": f"sha256:{digest}", "store_ref": digest, "media_type": media_type, "bytes": len(data)}

    def can_read_raw(self, role_id: str) -> bool:
        return "read_raw" in self._roles.get(role_id, frozenset())

    def read_artifact(self, role_id: str, ref: dict[str, Any], *, maximum_bytes: int) -> bytes:
        self._check(role_id, "read_raw")
        digest = ref.get("store_ref", "")
        if (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or ref.get("content_sha256") != f"sha256:{digest}"):
            raise ManifestError("invalid content-addressed evidence reference")
        with self._safe_path(digest).open("rb") as stream:
            data = stream.read(maximum_bytes + 1)
        if len(data) > maximum_bytes or len(data) != ref.get("bytes") or hashlib.sha256(data).hexdigest() != digest:
            raise ManifestError("artifact size or digest does not match its receipt")
        return data
