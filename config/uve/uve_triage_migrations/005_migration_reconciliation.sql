CREATE TABLE migration_reconciliations (
  reconciliation_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, ok INTEGER NOT NULL,
  report_sha256 TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE advisor_snapshots (
  run_id TEXT PRIMARY KEY REFERENCES runs, source_kind TEXT NOT NULL, captured_at TEXT NOT NULL,
  proposal_count INTEGER NOT NULL, observed_proposal_count INTEGER NOT NULL, fitness_record_count INTEGER NOT NULL,
  last_generation INTEGER, generation_summary_json TEXT
);
