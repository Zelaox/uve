"""SQLite SSOT for UVE test expansion proposals, fitness, and compaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_storage_policy(pipeline_root: Path) -> dict[str, Any]:
    path = pipeline_root / "config/uve/uve_evolution_storage_policy.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_schema_sql(pipeline_root: Path) -> str:
    policy = load_storage_policy(pipeline_root)
    ref = policy["schema_sql_ref"]
    return (pipeline_root / ref).read_text(encoding="utf-8")


def open_store(lab_root: Path, pipeline_root: Path, *, readonly: bool = False) -> tuple[sqlite3.Connection, Path]:
    policy = load_storage_policy(pipeline_root)
    rel = policy["database_relative_from_lab"]
    db_path = (lab_root / rel).resolve()
    timeout = policy["busy_timeout_ms"] / 1000
    if readonly:
        return sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=timeout), db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    try:
        if policy["wal_mode"]:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("BEGIN IMMEDIATE;\n" + load_schema_sql(pipeline_root) + "\nCOMMIT;")
    except BaseException:
        conn.close()
        raise
    return conn, db_path


def integrity_check(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row and row[0] == "ok")


def upsert_proposal(conn: sqlite3.Connection, record: dict[str, Any], *, transition: bool = False) -> bool:
    proposal_id = str(record["proposal_id"])
    update = """
        status=excluded.status, proposal_outcome=excluded.proposal_outcome,
        gap_confidence=excluded.gap_confidence, record_json=excluded.record_json,
        updated_at=excluded.updated_at
    """
    if not transition:
        update += """
            WHERE proposals.status = 'proposed'
              AND json_remove(proposals.record_json, '$.created_at') !=
                  json_remove(excluded.record_json, '$.created_at')
        """
    return bool(conn.execute(
        """
        INSERT INTO proposals (proposal_id, parent_scenario, strategy_id, status,
                               proposal_outcome, gap_confidence, record_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(proposal_id) DO UPDATE SET
        """ + update,
        (
            proposal_id,
            str(record["parent_scenario"]),
            str(record["strategy_id"]),
            str(record.get("status") or "proposed"),
            str(record.get("proposal_outcome") or "proposed"),
            str(record.get("gap_confidence") or "suspected"),
            json.dumps(record, ensure_ascii=False),
            _utc_now(),
        ),
    ).rowcount)


def count_proposals(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()
    return int(row[0]) if row else 0


def simulation_storage_growth(
    lab_root: Path,
    pipeline_root: Path,
    *,
    scenarios: int,
    generations: int,
) -> dict[str, Any]:
    policy = load_storage_policy(pipeline_root)
    probe_rel = str(policy.get("storage_probe_relative_from_lab") or "logs/uve/expansion_regression_probe")
    run_id = uuid.uuid4().hex
    probe_root = (lab_root / probe_rel / run_id).resolve()
    probe_root.mkdir(parents=True, exist_ok=True)
    db_path = probe_root / "probe.sqlite"
    before_files = sum(1 for _ in probe_root.rglob("*") if _.is_file())
    conn = sqlite3.connect(str(db_path))
    if policy.get("wal_mode"):
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(load_schema_sql(pipeline_root))
    for gen in range(generations):
        for scenario_index in range(scenarios):
            record = {
                "proposal_id": hashlib.sha256(f"{gen}:{scenario_index}".encode()).hexdigest()[:32],
                "parent_scenario": f"scenario_{scenario_index}",
                "strategy_id": f"probe_{gen}_{scenario_index}",
                "status": "proposed",
                "proposal_outcome": "proposed",
                "gap_confidence": "suspected",
                "project_id": "ailab_engine",
                "priority": 0.5,
                "rationale": "growth probe",
                "evidence_strength": "none",
                "static_analysis_only": True,
            }
            upsert_proposal(conn, record)
        conn.commit()
    conn.close()
    after_files = sum(1 for _ in probe_root.rglob("*") if _.is_file())
    db_bytes = db_path.stat().st_size if db_path.is_file() else 0
    max_files = int(policy.get("max_new_files_per_regression") or 8)
    max_bytes = int(policy.get("max_db_bytes_regression") or 52428800)
    ok = after_files - before_files <= max_files and db_bytes <= max_bytes
    return {
        "ok": ok,
        "new_files": after_files - before_files,
        "max_new_files": max_files,
        "db_bytes": db_bytes,
        "max_db_bytes": max_bytes,
        "probe_root": str(probe_root),
    }


def simulate_interrupted_write(lab_root: Path, pipeline_root: Path) -> dict[str, Any]:
    policy = load_storage_policy(pipeline_root)
    probe_rel = str(policy.get("storage_probe_relative_from_lab") or "logs/uve/expansion_regression_probe")
    rel_db = f"{probe_rel}/{uuid.uuid4().hex}/integrity.sqlite"
    db_path = (lab_root / rel_db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    if policy.get("wal_mode"):
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(load_schema_sql(pipeline_root))
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO proposals (proposal_id, parent_scenario, strategy_id, status, "
        "proposal_outcome, gap_confidence, record_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("partial1", "s", "st", "proposed", "proposed", "suspected", "{}", _utc_now()),
    )
    conn.commit()
    check_ok = integrity_check(conn)
    conn.close()
    return {"ok": check_ok, "db_path": str(db_path)}
