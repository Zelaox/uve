CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY, binding_manifest_ref TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
  run_kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL, correlation_json TEXT NOT NULL DEFAULT '{}', parent_run_id TEXT REFERENCES runs
);
CREATE INDEX runs_project_time ON runs(project_id, started_at DESC, run_id);
CREATE TABLE run_links (
  from_run_id TEXT REFERENCES runs, to_run_id TEXT REFERENCES runs, link_kind TEXT NOT NULL,
  CHECK(from_run_id != to_run_id), PRIMARY KEY(from_run_id, to_run_id, link_kind)
);
CREATE TABLE evidence_refs (
  evidence_id TEXT PRIMARY KEY, root_kind TEXT NOT NULL, uri_relative TEXT NOT NULL,
  content_sha256 TEXT NOT NULL, trust_tier TEXT NOT NULL, authority_ref TEXT NOT NULL,
  byte_size INTEGER NOT NULL, media_type TEXT NOT NULL,
  UNIQUE(root_kind, uri_relative, content_sha256)
);
CREATE TABLE run_evidence (
  run_id TEXT REFERENCES runs, evidence_id TEXT REFERENCES evidence_refs, role TEXT NOT NULL,
  PRIMARY KEY(run_id, evidence_id, role)
);
CREATE TABLE events (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs, observed_at TEXT NOT NULL,
  subsystem TEXT, event_name TEXT NOT NULL, entity_ref TEXT, payload_json TEXT NOT NULL,
  source_ref TEXT NOT NULL REFERENCES evidence_refs, content_sha256 TEXT NOT NULL
);
CREATE INDEX events_kind ON events(run_id, subsystem, event_name);
CREATE TABLE findings (
  finding_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
  diagnostic_code TEXT NOT NULL, severity TEXT NOT NULL, certainty TEXT NOT NULL,
  finding_fingerprint TEXT NOT NULL UNIQUE, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  status TEXT NOT NULL, entity_ref TEXT, detail_json TEXT NOT NULL
);
CREATE INDEX findings_code ON findings(diagnostic_code,severity);
CREATE TABLE finding_occurrences (
  occurrence_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL REFERENCES findings,
  run_id TEXT NOT NULL REFERENCES runs, evidence_epoch TEXT NOT NULL,
  source_evidence_id TEXT NOT NULL REFERENCES evidence_refs, count INTEGER NOT NULL CHECK(count > 0)
);
CREATE INDEX occurrences_run ON finding_occurrences(run_id, finding_id);
CREATE TABLE finding_evidence (
  finding_id TEXT REFERENCES findings, evidence_id TEXT REFERENCES evidence_refs, role TEXT NOT NULL,
  PRIMARY KEY(finding_id,evidence_id,role)
);
CREATE TABLE probes (
  probe_ref TEXT PRIMARY KEY, profile_ref TEXT, manifest_sha256 TEXT
);
CREATE TABLE probe_results (
  probe_run_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
  probe_ref TEXT NOT NULL REFERENCES probes, outcome TEXT NOT NULL, failure_class TEXT,
  gate_receipt_ref TEXT REFERENCES evidence_refs, receipt_ref TEXT NOT NULL REFERENCES evidence_refs,
  subject_identity_hash TEXT, verified_at TEXT, reported_verdict TEXT NOT NULL,
  CHECK(outcome != 'pass' OR gate_receipt_ref IS NOT NULL)
);
CREATE TABLE gate_links (
  probe_run_id TEXT REFERENCES probe_results, gate_receipt_ref TEXT REFERENCES evidence_refs,
  gate_id TEXT NOT NULL, decision TEXT NOT NULL, checked INTEGER NOT NULL CHECK(checked IN (0,1)),
  observed_at TEXT NOT NULL, PRIMARY KEY(probe_run_id,gate_receipt_ref)
);
CREATE TABLE resolution_links (
  finding_fingerprint TEXT REFERENCES findings(finding_fingerprint),
  probe_run_id TEXT NOT NULL REFERENCES probe_results, gate_receipt_ref TEXT NOT NULL REFERENCES evidence_refs,
  verified_at TEXT NOT NULL, PRIMARY KEY(finding_fingerprint,gate_receipt_ref),
  FOREIGN KEY(probe_run_id,gate_receipt_ref) REFERENCES gate_links(probe_run_id,gate_receipt_ref)
);
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY, parent_scenario TEXT, strategy_id TEXT, status TEXT,
  proposal_outcome TEXT, gap_confidence REAL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  source_ref TEXT NOT NULL REFERENCES evidence_refs
);
CREATE TABLE proposal_evaluations (
  evaluation_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals,
  generation INTEGER, variant_id TEXT, inputs_hash TEXT, record_json TEXT NOT NULL
);
CREATE TABLE fitness_records (
  fitness_id TEXT PRIMARY KEY, strategy_id TEXT, proposal_id TEXT REFERENCES proposals,
  generation INTEGER, record_json TEXT NOT NULL
);
CREATE TABLE evolution_generations (generation INTEGER PRIMARY KEY, summary_json TEXT NOT NULL);
CREATE TABLE proposal_history (
  history_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals, action TEXT NOT NULL,
  actor_ref TEXT, at TEXT, linked_probe_run_id TEXT REFERENCES probe_results, detail_json TEXT NOT NULL
);
CREATE TABLE finding_proposals (
  finding_fingerprint TEXT REFERENCES findings(finding_fingerprint), proposal_id TEXT REFERENCES proposals,
  PRIMARY KEY(finding_fingerprint,proposal_id)
);
CREATE TABLE run_summaries (
  run_id TEXT PRIMARY KEY REFERENCES runs ON DELETE RESTRICT, project_id TEXT NOT NULL REFERENCES projects,
  rollup_status TEXT NOT NULL, error_count INTEGER NOT NULL, warning_count INTEGER NOT NULL,
  info_count INTEGER NOT NULL, schema_issue_count INTEGER NOT NULL, root_cause_status TEXT NOT NULL,
  most_affected_entity_ref TEXT, suggested_action_ref TEXT, regression_status TEXT,
  summary_text TEXT NOT NULL, summary_generated_at TEXT NOT NULL, generator_ref TEXT NOT NULL,
  summary_complete INTEGER NOT NULL CHECK(summary_complete IN(0,1)), detail_json TEXT NOT NULL
);
CREATE INDEX summaries_health ON run_summaries(rollup_status,summary_complete);
CREATE TABLE error_summaries (
  error_summary_id TEXT PRIMARY KEY, finding_fingerprint TEXT NOT NULL UNIQUE REFERENCES findings(finding_fingerprint),
  diagnostic_code TEXT NOT NULL, short_label TEXT NOT NULL, likely_cause_hypothesis TEXT,
  recommended_action_ref TEXT, lifecycle_status TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, total_occurrences INTEGER NOT NULL, affected_projects_json TEXT NOT NULL
);
CREATE INDEX error_summary_code ON error_summaries(diagnostic_code,lifecycle_status);
CREATE TABLE summary_evidence_refs (
  summary_key TEXT NOT NULL, run_id TEXT REFERENCES run_summaries, error_summary_id TEXT REFERENCES error_summaries,
  evidence_id TEXT REFERENCES evidence_refs, finding_id TEXT REFERENCES findings, field_name TEXT NOT NULL,
  certainty TEXT NOT NULL, CHECK((run_id IS NULL) != (error_summary_id IS NULL)),
  CHECK(evidence_id IS NOT NULL OR finding_id IS NOT NULL), PRIMARY KEY(summary_key)
);
CREATE TABLE run_comparisons (
  comparison_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
  run_id_a TEXT NOT NULL REFERENCES runs, run_id_b TEXT NOT NULL REFERENCES runs,
  compared_at TEXT NOT NULL, comparison_manifest_sha256 TEXT NOT NULL, manifest_ref TEXT NOT NULL,
  content_hash TEXT NOT NULL, UNIQUE(run_id_a,run_id_b,comparison_manifest_sha256), CHECK(run_id_a != run_id_b)
);
CREATE INDEX comparison_project ON run_comparisons(project_id,compared_at DESC);
CREATE TABLE run_comparison_items (
  item_id TEXT PRIMARY KEY, comparison_id TEXT NOT NULL REFERENCES run_comparisons ON DELETE CASCADE,
  finding_fingerprint TEXT NOT NULL REFERENCES findings(finding_fingerprint), category TEXT NOT NULL,
  occurrences_a INTEGER NOT NULL, occurrences_b INTEGER NOT NULL, delta INTEGER NOT NULL,
  lifecycle_status_after TEXT NOT NULL, linked_gate_receipt_ref TEXT REFERENCES evidence_refs,
  detail_json TEXT NOT NULL, UNIQUE(comparison_id,finding_fingerprint),
  CHECK(lifecycle_status_after != 'resolved' OR linked_gate_receipt_ref IS NOT NULL)
);
CREATE INDEX comparison_item_fp ON run_comparison_items(finding_fingerprint,category);
CREATE INDEX comparison_item_cat ON run_comparison_items(comparison_id,category);
CREATE TABLE ingest_batches (
  batch_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, actor_class_ref TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, rows_in INTEGER NOT NULL, rows_out INTEGER NOT NULL,
  integrity_check_ok INTEGER NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE indexed_sources (
  source_type TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES evidence_refs,
  batch_id TEXT NOT NULL REFERENCES ingest_batches, PRIMARY KEY(source_type,evidence_id)
);
CREATE TABLE compaction_runs (
  compaction_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  outcome TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE evidence_holds (
  hold_id TEXT PRIMARY KEY, evidence_id TEXT REFERENCES evidence_refs,
  root_kind TEXT, uri_relative TEXT, reason_ref TEXT NOT NULL, released_at TEXT,
  CHECK(evidence_id IS NOT NULL OR (root_kind IS NOT NULL AND uri_relative IS NOT NULL))
);
