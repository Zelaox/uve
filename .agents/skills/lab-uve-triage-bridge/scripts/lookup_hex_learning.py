"""Bounded, non-migrating lookups in the configured Hex Editor Learning DB."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import re
import sqlite3
import sys
from pathlib import Path


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
        raise ValueError("Invalid configured SQL identifier")
    return '"' + value + '"'


def lookup(bundle, db_path, *, identity=None, filters=None, tables=None, limit=None):
    """Read existing checkpointed bytes only; rows remain unverified index hints."""
    policy = bundle["policy"]
    filters = dict(filters or {})
    if identity:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", identity):
            raise ValueError("identity-sha256 must contain 64 hexadecimal characters")
        identity = identity.lower()
    if not identity and not filters:
        raise ValueError("Provide an identity hash or at least one exact filter")
    chosen_limit = int(policy["queries"]["default_limit"] if limit is None else limit)
    if not 1 <= chosen_limit <= int(policy["queries"]["max_limit"]):
        raise ValueError("Query limit is outside the configured policy bounds")
    specs = policy["tables"]
    requested = list(dict.fromkeys(tables or specs))
    if any(table not in specs for table in requested):
        raise ValueError("Unknown policy table")
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        return {"ok": False, "status": "not_found", "results": {}}
    wal_path = db_path.with_name(db_path.name + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size:
        return {"ok": False, "status": "requires_checkpointed_snapshot", "results": {}}
    # Match every declared identity role, including candidate outputs and parser inputs.
    identity_fields = set(policy.get("identity_lookup", {}).get("runs_sha_columns", []))
    identity_fields.update({"source_sha256", "source_digest", "parent_sha256", "candidate_sha256"})
    output_field = policy.get("identity_lookup", {}).get("candidate_output_column")
    if output_field:
        identity_fields.add(output_field)
    results = {}
    skipped = {}
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        available = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in requested:
            spec = specs[table]
            declared = {column["name"] for column in spec["columns"]}
            roles = sorted(declared & identity_fields)
            if set(filters) - declared or (identity and not roles):
                skipped[table] = "selectors_not_supported"
                continue
            if table not in available:
                results[table] = {"status": "table_unavailable", "rows": []}
                continue
            actual = {row[1] for row in connection.execute("PRAGMA table_info(" + identifier(table) + ")")}
            required = set(filters) | set(roles if identity else [])
            if required - actual:
                results[table] = {"status": "schema_unavailable", "rows": []}
                continue
            selected = [column["name"] for column in spec["columns"] if column["name"] in actual]
            clauses, values = [], []
            if identity:
                clauses.append("(" + " OR ".join(identifier(role) + " = ?" for role in roles) + ")")
                values.extend([identity] * len(roles))
            for key, value in filters.items():
                clauses.append(identifier(key) + " = ?")
                values.append(value)
            order = []
            if "indexed_at" in actual:
                order.append('"indexed_at" DESC')
            order.extend(identifier(key) for key in spec["primary_key"] if key in actual)
            sql = "SELECT " + ",".join(identifier(key) for key in selected) + " FROM " + identifier(table)
            sql += " WHERE " + " AND ".join(clauses)
            if order:
                sql += " ORDER BY " + ",".join(order)
            rows = [dict(row) for row in connection.execute(sql + " LIMIT ?", [*values, chosen_limit + 1])]
            results[table] = {
                "status": "completed", "rows": rows[:chosen_limit],
                "count": min(len(rows), chosen_limit), "truncated": len(rows) > chosen_limit,
                "identity_columns": roles if identity else [],
            }
    # Refuse a concurrently appearing WAL rather than presenting an incomplete index as current.
    if wal_path.is_file() and wal_path.stat().st_size:
        return {"ok": False, "status": "requires_checkpointed_snapshot", "results": {}}
    complete = bool(results) and all(result["status"] == "completed" for result in results.values())
    status = "completed" if complete else ("partial" if results else "selectors_not_supported")
    return {
        "ok": complete, "status": status, "db_path": str(db_path),
        "match_kind": "exact_identity" if identity else "related_metadata",
        "identity_sha256": identity, "filters": filters, "limit_per_table": chosen_limit,
        "results": results, "skipped_tables": skipped,
        "reuse_authority": "index_hint_only",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-root", type=Path, required=True)
    parser.add_argument("--identity-sha256")
    parser.add_argument("--filter", action="append", default=[], metavar="COLUMN=VALUE")
    parser.add_argument("--table", action="append")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        lab_root = args.lab_root.resolve()
        config = json.loads((lab_root / "config/hex_editor.json").read_text(encoding="utf-8-sig"))
        editor_root = (lab_root / config["application_path"]).resolve()
        if not (editor_root / "src/services/learning_db/core.py").is_file():
            raise ValueError("Configured Hex Editor learning service is unavailable")
        sys.path.insert(0, str(editor_root))
        from src.services.learning_db import learning_db_path, load_learning_db_bundle

        filters = {}
        for pair in args.filter:
            if "=" not in pair:
                raise ValueError("--filter requires COLUMN=VALUE")
            key, value = pair.split("=", 1)
            if not key or key in filters:
                raise ValueError("Filter column is empty or duplicated")
            filters[key] = value
        bundle = load_learning_db_bundle()
        result = lookup(bundle, learning_db_path(bundle=bundle), identity=args.identity_sha256,
                        filters=filters, tables=args.table, limit=args.limit)
    except (OSError, ValueError, KeyError, TypeError, ImportError, sqlite3.Error) as exc:
        result = {"ok": False, "status": "lookup_unavailable", "error": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
