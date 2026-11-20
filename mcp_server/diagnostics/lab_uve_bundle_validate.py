"""Validate a lab_uve_incident_bundle.v1 document (UVE command_check target)."""

from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from mcp_server.diagnostics.lab_uve_bridge import BRIDGE_MANIFEST_REL, load_bridge_manifest, load_ref


def _fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def validate_bundle_dict(repo_root: Path, bundle: dict[str, Any]) -> None:
    bridge = load_bridge_manifest(repo_root)
    expected_schema = bridge.get("bundle_schema_version")
    if bundle.get("schema_version") != expected_schema:
        _fail(f"schema_version must be {expected_schema}")
    if bundle.get("mode") != "read_only_sources":
        _fail("mode must be read_only_sources")
    for key in ("logical_incident_id", "occurrence_id", "evidence_epoch", "correlation"):
        if key not in bundle:
            _fail(f"missing required field: {key}")
    repair = load_ref(repo_root, bridge["repair_authorization_contract_ref"])
    if repair.get("bundle_reducer_may_execute_repair"):
        _fail("repair contract must forbid bundle repair execution")
    if bundle.get("repair_executed"):
        _fail("bundle must not claim repair_executed")


def validate_bundle_file(repo_root: Path, bundle_path: Path) -> None:
    if not bundle_path.is_file():
        _fail(f"bundle file missing: {bundle_path}")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    validate_bundle_dict(repo_root, bundle)


def main() -> int:
    repo_root = Path(os.environ.get("UVE_AI_LAB_ROOT") or "").resolve()
    bundle_env_key = "LAB_UVE_INCIDENT_BUNDLE_PATH"
    if not repo_root.is_dir():
        bridge_path = Path(__file__).resolve().parents[2] / BRIDGE_MANIFEST_REL
        if bridge_path.is_file():
            repo_root = bridge_path.parents[1].resolve()
    binding_path = repo_root / "config" / "lab_uve_uve_binding.json"
    if binding_path.is_file():
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        bundle_env_key = str(binding.get("bundle_validate_env_key") or bundle_env_key)
    bundle_arg = ""
    if len(sys.argv) >= 3 and sys.argv[1] == "--bundle":
        bundle_arg = sys.argv[2]
    bundle_path = Path(os.environ.get(bundle_env_key) or bundle_arg or "").resolve()
    if not bundle_path.is_file():
        _fail(f"bundle path not set ({bundle_env_key}) or file missing")
    raw = bundle_path.read_bytes()
    validate_bundle_dict(repo_root, json.loads(raw))
    from mcp_server.diagnostics.uve_triage_store import load_config

    token = load_config(repo_root).policy("proof_ref")["bundle_hash_output_token"]
    print("BUNDLE_VALID")
    print(f"{token} sha256:{hashlib.sha256(raw).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
