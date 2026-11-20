# UVE storage lifecycle on Windows

The lifecycle manifest ships disabled. From the Lab root, preview the scheduler
definition with `scripts/uve/install_storage_lifecycle_task.ps1`. After separate
activation approval and an enabled manifest, run the installer with `-Register`.
The installer replaces the task with the same manifest-defined name. Inspect its
trigger, action and history in Task Scheduler (`taskschd.msc`).

The scheduled action runs a dry-run inventory with the maintenance actor class.
Machine idle, battery settings and the daily trigger determine when the task can
run. The Lab clock uses eligible activity markers. Windows Storage Sense is a
separate system facility. Keep the Lab outside its managed cleanup locations.

Set the Lab root environment variable declared in the UVE binding and run these
commands from the bound UVE pipeline:

```powershell
python -B -m agent_training.uve.cli storage config
python -B -m agent_training.uve.cli storage scan --mode dry_run
python -B -m agent_training.uve.cli storage health
python -B -m agent_training.uve.cli storage inactivity status
```

After a long absence, record actual Lab use or run `storage activity ack`. Run
`storage return reconcile` and review its inventory ID and plan hash. Record consent
with `storage purge approve --inventory-run-id <id> --confirm-manifest-sha256 <sha>
--approver-ref <operator>`. Approval is tied to that inventory and expires.

An approved quarantine execution uses `storage execute --mode quarantine` with the
inventory ID, `--plan-sha256`, manifest confirmation and any required approval ID.
Quarantined files stay in the manifest's quarantine directory for the configured
hold period. To prepare a purge, use `storage plan --mode purge` with those
inventory arguments and review the returned request file. Set the request
environment variable from the verification manifest to that file. Run the
declared UVE profile and its command probe, then have the independent evaluator
evaluate the declared gate. Pass its receipt via `--gate-receipt` to
`storage execute --mode purge` with the same inventory arguments.

Review the audit for `quarantine_prepared` or `purge_prepared` after an interrupted
execution. Prepared actions require operator reconciliation before their files
can become eligible again.

Agents writing to the configured scratch roots can use `storage lease acquire`
with `--work-root`, `--actor-ref` and the writer's `--pid`. Keep it live with
`storage lease heartbeat --lease-id <id>` and release after writes close.
The codegen write gate wraps manifest-scoped project roots with the same lease
and heartbeat. Other Lab roots remain outside cleanup scope.
