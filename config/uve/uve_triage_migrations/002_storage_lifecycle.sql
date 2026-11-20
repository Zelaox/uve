CREATE TABLE cleanup_runs (
  cleanup_run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, actor_class_ref TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
  bytes_scanned INTEGER NOT NULL, bytes_reclaimable INTEGER NOT NULL, bytes_quarantined INTEGER NOT NULL DEFAULT 0,
  bytes_purged INTEGER NOT NULL DEFAULT 0, protected_count INTEGER NOT NULL, aborted INTEGER NOT NULL,
  abort_reason TEXT, reconciliation_kind TEXT, plan_sha256 TEXT NOT NULL, storage_health_json TEXT NOT NULL
);
CREATE TABLE cleanup_candidates (
  candidate_id TEXT PRIMARY KEY, cleanup_run_id TEXT NOT NULL REFERENCES cleanup_runs,
  uri_relative TEXT NOT NULL, root_kind TEXT NOT NULL, data_class_ref TEXT NOT NULL,
  age_seconds INTEGER NOT NULL, byte_size INTEGER NOT NULL, content_sha256 TEXT,
  reference_count INTEGER NOT NULL, hold_ids_json TEXT NOT NULL, decision TEXT NOT NULL, decision_reason_ref TEXT NOT NULL
);
CREATE INDEX cleanup_candidate_scan ON cleanup_candidates(cleanup_run_id,decision);
CREATE TABLE cleanup_actions (
  action_id TEXT PRIMARY KEY, cleanup_run_id TEXT NOT NULL REFERENCES cleanup_runs,
  candidate_id TEXT REFERENCES cleanup_candidates, action_kind TEXT NOT NULL, executed_at TEXT,
  source_uri TEXT NOT NULL, dest_uri TEXT, reversible INTEGER NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE storage_health_snapshots (
  snapshot_id TEXT PRIMARY KEY, cleanup_run_id TEXT NOT NULL REFERENCES cleanup_runs,
  captured_at TEXT NOT NULL, bytes_analyzed INTEGER NOT NULL, bytes_reclaimable_estimate INTEGER NOT NULL,
  protected_artifact_count INTEGER NOT NULL, quarantine_bytes INTEGER NOT NULL, corpus_note TEXT NOT NULL
);
CREATE TABLE active_run_leases (
  lease_id TEXT PRIMARY KEY, resource_kind_ref TEXT NOT NULL, actor_ref TEXT NOT NULL, work_root_uri_relative TEXT NOT NULL,
  artifact_glob_refs_json TEXT NOT NULL, linked_run_id TEXT, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  lease_store_receipt_ref TEXT NOT NULL, pid INTEGER NOT NULL, process_created_at REAL, released_at TEXT
);
CREATE TABLE lab_activity_markers (
  marker_id TEXT PRIMARY KEY, activity_kind_ref TEXT NOT NULL, observed_at TEXT NOT NULL,
  source_ref TEXT NOT NULL, actor_class_ref TEXT NOT NULL, inactivity_clock_eligible INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL
);
CREATE INDEX lab_activity_clock ON lab_activity_markers(inactivity_clock_eligible,observed_at DESC);
CREATE TABLE inactivity_protection_state (
  state_id TEXT PRIMARY KEY, paused INTEGER NOT NULL, paused_since TEXT, last_activity_at TEXT,
  inactivity_tier_ref TEXT NOT NULL, reason_ref TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
  post_return_reconcile_required INTEGER NOT NULL,
  last_post_return_inventory_run_id TEXT REFERENCES cleanup_runs
);
CREATE TABLE lifecycle_purge_approvals (
  approval_id TEXT PRIMARY KEY, approved_at TEXT NOT NULL, approver_ref TEXT NOT NULL,
  post_return_inventory_cleanup_run_id TEXT NOT NULL REFERENCES cleanup_runs,
  manifest_sha256 TEXT NOT NULL, expires_at TEXT NOT NULL, detail_json TEXT NOT NULL
);
