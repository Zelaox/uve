# TRIAGE observability

The standalone TRIAGE implementation lives in `mcp_server/diagnostics/uve_triage_*.py`; the incident-bundle implementation is in `lab_uve_*.py`. Run its module commands from this checkout. The exported binding `config/lab_uve_uve_binding.json` selects `.` as the UVE pipeline. An external AI Lab checkout uses its own binding instead.

The index stores metadata, hashes, normalized findings, comparisons and evidence links. Original source files remain authoritative. A source hash proves byte identity, not authorship, completeness or a repaired application.

## Ingest and query

Review `config/uve_triage_storage_manifest.json` and its ingestion routes before importing local reports. Ingestion initializes/migrates the configured SQLite index and appends derived index state. It does not repair or edit source reports.

```powershell
python -B -m mcp_server.diagnostics.uve_triage_ingest --batch operator-import
python -B -m mcp_server.diagnostics.uve_triage_query project-health --project-id PROJECT
python -B -m mcp_server.diagnostics.uve_triage_query run-summary --run-id RUN_ID
python -B -m mcp_server.diagnostics.uve_triage_query trace-finding --fingerprint FINGERPRINT
python -B -m mcp_server.diagnostics.uve_triage_query fingerprint-timeline --fp FINGERPRINT
```

The read operations open the store in read-only/query-only mode. Missing stores, inaccessible WAL/SHM files, schema gaps and unavailable evidence are errors to retain, not empty successful results. Importing an empty checkout creates no historical incidents.

`scripts/debug/inventory_uve_triage_sources.py --write-report` inventories the configured sources. Reconciliation and cutover reports compare the metadata projection with source identities. The adapter's `read_primary` setting governs advisor cutover; do not infer cutover from a successful read query.

## Comparisons and proof

Advisor cutover requires the policy's recorded seven-day parallel reconciliation window; it is not enabled by an isolated successful import.

```powershell
python -B -m mcp_server.diagnostics.uve_triage_query diff-runs --a FIRST --b SECOND
python -B -m mcp_server.diagnostics.uve_triage_query comparison-show --comparison-id COMPARISON
```

`diff-runs` persists a comparison. `not_observed` means a finding is absent in the compared run. A `resolved` finding needs a configured resolution contract and independent UVE gate bound to that fingerprint. A registry match, a passing bundle validator or an empty query does not establish resolution.

For indexed UVE runs, `run-summary` includes `receipt_explanations`. The reader checks the original receipt's indexed size and hash before returning its explanation. `legacy_receipt` and `evidence_unavailable` are explicit states.

Generic probe/gate run summaries are deliberately conservative and can show `UVE_NOT_VERIFIED` while the original receipt records `passed` or `approved`. Gate links are checked separately through the configured UVE engine. Inspect the original receipt, its scope and current gate validation rather than promoting a rollup label into a new verdict.

On adapter-enabled projects, `probe run --index` and `gate evaluate --index` mirror saved receipts through the bound sidecar. Set the Lab-root environment key from the binding. Index failures do not edit the original UVE outcome or turn an unverified capability into an approval.

## Storage lifecycle

`python -B -m agent_training.uve.cli storage --help` exposes inventory and lifecycle operations. Policies declare roots, holds, leases, activity, retention and approved execution. Scanning reports candidates; it does not authorize deletion. Keep live writer leases, failed-run evidence holds and source hashes intact. Do not invoke mutation or scheduler actions merely to inspect an incident.

## External integrations

The published core includes the index, bundle bridge, storage policies and their SQL migrations. AI Lab dashboard/codegen reducers, native engine/game contracts, training checkpoint quality runners and dataset publication remain in the projects that own their runtime and data. Their bindings may be present as optional examples; this checkout does not claim to provide those applications.

The [unified skill](../.agents/skills/lab-uve-triage-bridge/SKILL.md) covers indexed history, reverse engineering with an external Hex Editor, and live forensic evidence. It requires real acceptance for each claim and reports unsupported coverage as `not_verified`.
