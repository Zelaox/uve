-- ailab.uve.evolution_storage_schema.v1
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY,
  parent_scenario TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  status TEXT NOT NULL,
  proposal_outcome TEXT NOT NULL,
  gap_confidence TEXT NOT NULL,
  record_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
DROP INDEX IF EXISTS idx_proposals_fingerprint;
CREATE INDEX IF NOT EXISTS idx_proposals_parent_strategy
  ON proposals (parent_scenario, strategy_id);
CREATE TABLE IF NOT EXISTS proposal_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  inputs_hash TEXT NOT NULL,
  record_json TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);
CREATE TABLE IF NOT EXISTS fitness_records (
  fitness_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  proposal_id TEXT,
  generation INTEGER NOT NULL,
  defect_discovery_fitness REAL NOT NULL,
  preventive_coverage_value REAL NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_summaries (
  generation INTEGER PRIMARY KEY,
  summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_holds (
  hold_id TEXT PRIMARY KEY,
  evidence_bundle_id TEXT,
  proposal_id TEXT,
  incident_id TEXT,
  hold_until_generation INTEGER
);
CREATE TABLE IF NOT EXISTS compaction_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  rows_pruned INTEGER NOT NULL DEFAULT 0,
  holds_preserved INTEGER NOT NULL DEFAULT 0,
  integrity_check_ok INTEGER NOT NULL DEFAULT 0,
  aborted INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT
);
