# Universal Development Verification Engine (UVE)

UVE runs manifest-defined probes, records immutable results, evaluates independent gates, and exports training rows in `training_pipeline/`. The implementation plan and acceptance record are in [the UVE plan](plans/universal_development_verification_engine_20261028/plan.MD).

## Workstation startup

Opening `EmulatorWorkstation.exe` starts a hidden, standalone UVE worker. Closing the workstation signals the worker to stop; Windows Job Object ownership also terminates its process tree if the workstation exits abruptly. AI Lab and the Training GUI are optional tools and are not launched by this startup path.

SDK capture and managed worker launches inherit only their own standard handles. This prevents concurrent launches from retaining another command's output pipe and stalling device discovery when the workstation restarts alongside an already running guest.

SDK command capture waits for the client to exit, then reads a fixed snapshot of up to 8 MiB through a separate handle. An inherited writer can continue using its own file position. A timeout terminates the SDK client, and oversized or incomplete output is discarded.

Managed process trees enter a Windows Job Object before their launcher starts executing. Workstation closes the job before joining the output reader, including when the launcher has already exited. Development-server startup reports a port conflict when a listener remains after the project's own cleanup command.

The state store validates the entire JSON document before loading it. It saves the previous valid state as a backup through an atomic replacement. Recovery can then save a new primary file while preserving the valid backup.

Configuration is split between two authoritative manifests:

- `config/workstation/workstation_ui_polling.manifest.json` selects `verificationStartupManifest`. The referenced `workstation_verification.ini` declares enablement, interpreter/environment override, working directory, command arguments, shutdown budgets, and context/status/log paths. Installation-relative paths resolve from the executable's installation root; state paths resolve from the workstation log root. Python 3.11+ is required. The shipped manifest resolves `python.exe` through inherited/persisted PATH or registered installations, with `UVE_PYTHON_EXECUTABLE` as a strict optional override. Minimum registered version and startup/status budgets are manifest settings.
- `training_pipeline/config/uve/uve_workstation_runtime.json` declares tiers, probes, gates, allowed adapters, profile/templates, leases and polling/reverification intervals. The shipped policy runs V1 engine structure, V2 selected-device ADB transport, and V3 Android boot plus a focused-application assertion. These are workstation checks; AI Lab checkpoint quality remains an explicitly invoked adapter.

SDK root, selected device serial, project root and application package come from the workstation's current discovery/selection state. The worker materializes target manifests from that context, reruns when it changes, and otherwise follows the configured verification interval. It does not choose a fallback device or start a guest/application. Without a selected device, V2/V3 remain `not_verified`; without a package, the V3 application assertion remains `not_verified`.

Android discovery parses exact serial/state fields from `adb devices -l`, matches the selected AVD through its reported id/name, and reconciles missing or reused online serials on subsequent observations. Multiple online transports reporting the same selected AVD identity leave the binding empty with an explicit diagnostic. Known offline/unauthorized transports retain their observed state. The context also includes `device_state` and `device_detail`; a selected AVD can legitimately have no serial while powered off.

The worker also discovers the selected project's `config/uve/application.json` contract. Application acceptance is separate from the engine/ADB/boot tiers and requires `application_code`, `application_bundle` and `application_health`. Missing contracts or unavailable inputs remain `not_verified`. A passing package-focus or Metro status check cannot approve application code. ProTrainer's contract declares whole-app/shared-module static checks, behavior regressions, actual entry/lazy-chunk bundling and positive project UI assertions. See that project's `docs/EMULATOR_APP_VERIFICATION.md` for its acceptance scope.

Every real `adb_ui_flow` probe is guarded by `application_fault_policy.json`: React Native error overlays, Metro resolution errors and error-boundary fallback screens fail even if old app content underneath satisfies a positive selector. The application-health adapter additionally reads bounded recent logcat messages for the selected package's actual PIDs; it never clears logs or examines another package's process history. Editable text is excluded from UI fault matching. The passive monitor performs no navigation or writes.

Application code receipts are reused only while current source/configuration/context hashes still validate. Changes invalidate the green application state and schedule fresh checks. Failed/unavailable code checks are not cached as success. The runtime publishes a heartbeat and separate application decision, and the Workstation title displays V1/V2/V3 plus App status. Status consumers reject stopped/dead workers, stale heartbeats and application receipts that no longer match current code. A startup failure replaces mutable status with `not_verified`, preserving immutable historical receipts.

The interpreter is resolved from the explicit override or inherited PATH, then registered Python installations and persisted Windows user/machine PATH. No machine-specific interpreter path is embedded. Native regression covers an empty inherited PATH and verifies that a missing explicit interpreter cannot leave old approved status behind. Isolated fault-injection acceptance covers theme/LogBox/Metro UI errors, recent versus historical fatal logs and a real JavaScript syntax mutation with an approved → rejected → approved gate sequence. Defined tests establish their stated behavior; they do not prove every application business rule.

The workstation log folder contains `verification/context.json`, `verification/status.json` and `verification/runner.log` under the shipped configuration. Detailed receipts and the canonical worker status are under the installed `training_pipeline/logs/uve/` directory. `python -m agent_training.uve.cli status` includes the runtime status. Startup failures are recorded as `verification_start_failed` in the workstation log. Setting `enabled=false` in the startup manifest disables the worker on the next launch. No AI Lab checkout, project path, emulator serial or machine-specific interpreter path is embedded in the startup code.

Runtime status includes each probe's observation, fault origin, failure class and receipt path, plus each gate's receipt path. Missing ADB bindings remain `not_verified` with `missing_fields`, rather than being described as an adapter crash. These details explain `insufficient_probes` without changing gate policy.

### Diagnose stored runtime evidence

Run from the installation's own `training_pipeline/` directory:

```powershell
python -B -m agent_training.uve.cli diagnose --context <workstation-log-root>/verification/context.json
# An explicit status file can also be supplied:
python -B -m agent_training.uve.cli diagnose --status-path <status.json> --context <context.json>
```

The default status location comes from `uve_workstation_runtime.json`; `--runtime-manifest` selects another configuration. The command reads gate/probe receipts from that pipeline's configured log root and reports detailed observations, receipt links, hash/id/subject consistency and whether the supplied context matches the status hash. It works with older status files that only contain probe summaries. Exit `2` means missing/inconsistent evidence or a changed supplied context; exit `0` means those diagnostic checks completed consistently, even if a gate is `not_verified`. An omitted context is reported as an unknown match. This command does not run ADB, restart services, evaluate gates or certify current source/delivery freshness. A running worker and a matching receipt chain alone do not establish application acceptance.

The [runtime binding and diagnostics plan](plans/uve_runtime_binding_and_diagnostics/plan.MD) tracks implementation, runtime acceptance and the separate Coach acceptance roadmap.

## Run and verify

From `training_pipeline/`, with Python 3.11+ and pytest installed:

```powershell
python -m agent_training.uve.cli probe run --profile config/uve/profiles/emulator_workstation_smoke.json --probe dry_echo
python -m agent_training.uve.cli gate evaluate --gate smoke_close_issue --probe-run-id <probe_run_id> --evaluator-ref <independent-evaluator>
python -m agent_training.uve.cli status
```

Probe exit codes are `0` for passed, `1` for failed and `2` for not verified. Gate exit codes use the same mapping for approved, rejected and not verified. `dry_echo` checks engine plumbing; it does not establish application correctness.

From the repository root, `./scripts/run-uve-smoke.ps1` runs the UVE/GUI tests and the independent witness. It stops on any failed command. Add `-InstallDependencies` to install the editable package and development dependencies first. Live local HTTP tests require loopback-network access.

The scoped `workstation_binding_acceptance.json` profile also covers native sources, tests, build/configuration and the runtime diagnostics documentation. From the source repository root, set `UVE_WORKSTATION_ROOT` to that root for the process, then run `probe run --profile config/uve/profiles/workstation_binding_acceptance.json --probe command_check --repair-actor-ref <actual-repair-actor>` from `training_pipeline/`. Its declared command configures/builds with CMake, runs CTest and runs the UVE/Training GUI regression selection. The configured build directory uses its existing CMake cache, or CMake's environment/toolchain selection on first configuration. A passed command receipt requires a separately assigned independent gate evaluator before claiming application acceptance.

## Profiles and commands

Policies, commands, timeouts, selectors and resource keys belong in JSON manifests. A profile declares the workspace root, watched/ignored files, probe targets, execution binding and leases. Workspace identity uses relative paths, file kinds and streamed content hashes. Profiles may reference an environment-pins document, knowledge-base manifest or an alternate identity-governor manifest. Watch paths cannot escape the workspace. Directory links require explicit exclusion; external links fail closed.

`command_check` runs a declared build, static analysis or scenario command in the selected workspace (or required snapshot). Set `command_probe_refs.command_check` to a JSON document containing:

```json
{
  "argv": ["{python}", "check.py"],
  "timeout_seconds": 30,
  "expected_returncode": 0,
  "expected_stdout": "checks passed"
}
```

`{python}` and `{workspace_root}` are supported argument substitutions. Commands run without a shell. A missing adapter, timeout, permission error, configuration drift or changed input produces `not_verified`, rather than a successful subject result.

```powershell
python -m agent_training.uve.cli plan --profile <profile.json> --write-inventory <inventory.json>
python -m agent_training.uve.cli plan --profile <profile.json> --previous-inventory <inventory.json>
python -m agent_training.uve.cli regression capture --baseline-id emulator_workstation_smoke
python -m agent_training.uve.cli regression compare --baseline-id emulator_workstation_smoke
```

Planning considers added, modified and deleted paths. Baseline capture requires an approved gate.

## Android and Metro

Set `UVE_PROTRAINER_ROOT` to the application workspace. Edit targets under `config/uve/targets/protrainer/` for its Metro port, ADB serial, guest package and UI flow.

`adb_ui_flow` supports `shell_property`, `shell_dump_window`, `assert_element`, `tap_element`, `input_text`, `swipe`, `keyevent` and `assert_activity` steps. Element selectors match Android UI hierarchy attributes such as `text`, `content-desc` and `resource-id`. Activity checks inspect resumed activities; window checks inspect focused windows. Declare state assertions after interactions: a successful tap cannot override a later failed state assertion. Empty flows and interaction-only flows cannot pass. The supplied ProTrainer flow is a boot/focused-package smoke check; application acceptance flows must declare their own navigation and state expectations.

Set `screenshot: true` in a flow to capture a supplementary PNG. Artifacts are stored by SHA-256 and referenced from the receipt; a screenshot never determines the verdict.

`adb_lifecycle` uses a profile's `adb_lifecycle_ref` to launch a declared AVD on an unused serial, observe boot, capture its real display, stop it and verify process/transport disappearance. It refuses an occupied serial or an AVD already running elsewhere. The manifest supplies the executable environment variable, launch arguments, boot property and all command/boot/stop/poll limits. Lease both the AVD id and serial. Startup failures retain the full launch log as a content-addressed artifact.

## Recovery and isolation

```powershell
python -m agent_training.uve.cli recovery --profile config/uve/profiles/protrainer_development.json --action restart_metro
```

Both profile recovery policy and the recovery manifest must allow the action. `recovery_action_refs` points to a command manifest with `start_argv`, optional `environment`, `stop_timeout_seconds`, `retest_attempts`, `retest_interval_seconds` and `log_filename`. Commands start hidden on Windows. UVE terminates only processes it owns in the current recovery process; it does not kill an unrelated pre-existing server. A CLI run can restart a server that has already stopped. Completion requires a nonempty reverification chain with passing final receipts. Recovery does not itself authorize issue closure.

Kernel locks exclude competing threads and processes, including callers sharing a role. Locks stay held through receipt creation and throughout recovery/retest chains. A process crash releases its locks; TTL metadata never permits stealing a live lock. Profiles can lease several resources; partial acquisition rolls back. Server keys identify the host/port across projects. Android serial/AVD, test database host/port/namespace, mutation work directory and witness instance resources are supported through `uve_resource_lock_manifest.json`. Caller-supplied lease tokens must own every required resource.

Required snapshots copy and verify watched bytes before execution, mark copied files read-only, and check their hashes afterward. Source edits or snapshot mutations prevent passing verification. This is input isolation, not an OS sandbox for arbitrary commands.

## AI Lab bridge

The adapter invokes AI Lab's existing `llm_model_trainer.training_quality.runner.run_quality_tiers` API in process. Reading a gate manifest is never reported as a tier pass. Set `UVE_AI_LAB_ROOT` to the checkout and provide `ai_lab_run_ref` on the profile, or set `UVE_AI_LAB_RUN_JSON`, to a document shaped as:

```json
{
  "run_path_relative": "training_runs/model/run",
  "checkpoint_relative": "training_runs/model/run/checkpoints/step-N",
  "training_state_relative": "training_runs/model/run/checkpoints/step-N/training_state.json",
  "runtime_contract_relative": "logs/.training_runtime_contract.json",
  "training_stage": "0",
  "tiers": ["V1"],
  "receipt_scope": "online_quality"
}
```

Paths are relative to the declared AI Lab root and cannot escape it. The live runner performs its normal AI Lab receipt/telemetry and published-training-binding writes; it does not promote the checkpoint through UVE. Use a checkpoint workspace profile watching its actual input files; `uve_checkpoint_identity_manifest.json` allows larger artifacts. Candidate identity and the original check results are retained in the UVE receipt.

For historical import, set `UVE_AI_LAB_TIER_JSON` to a `training_quality_receipt.v1` file. Historical import checks the evidence digest when present, preserves candidate identity and treats skipped/incomplete required checks as `not_verified`. It does not rerun the checkpoint and should not be used to claim fresh checkpoint promotion.

## Gates, issues and training export

Probe plans bind the manifest bundle and transitively referenced profile/target files. Gates reject stale policy, changed workspace inputs, duplicate/missing required probes and absent required execution bindings. `engine_degraded` prevents approval. Self-check verifies manifests, adapter availability and log-store writability. Probes may declare repair authors with repeatable `--repair-actor-ref`; those actors cannot evaluate their repair gate. Gate consumers revalidate referenced hashes and current inputs.

```powershell
python -m agent_training.uve.cli export-row --profile <profile.json> --gate <gate-id> --gate-receipt <gate.json> --failure-class assertion_mismatch
python -m agent_training.uve.cli witness run
python -m agent_training.uve.cli witness run --candidate-root <candidate-training-pipeline-directory>
```

The witness runs trusted contract tests against a separate copy of candidate code/configuration in a subprocess. Both build and installed Workstation runtimes include the tests declared by `uve_witness_manifest.json`. A failed candidate receives a rejected gate and preserves `last_approved.json`.

Verified exports require matching probe hashes, identity and gate references. Missing probes, forged metadata and changed receipt contents are rejected. Export applies secret placeholders across the row, preserving stack traces and source locations. Repeated occurrences of the same secret receive the same placeholder. Raw evidence remains unchanged; `repair_agent` is denied raw reads by the evidence-store API. Host-assigned roles and a protected witness/configuration deployment remain trust prerequisites: caller-provided role labels and Windows file mode bits do not isolate hostile processes running under the same OS account.

## GUI and live acceptance

The Training GUI shows engine health, latest probe/gate receipts and open verification issue counts. It polls `uve_status` even before training starts and reports unavailable status explicitly.

Acceptance artifacts are under `training_pipeline/logs/uve/acceptance/`; the checked-in [verification record](plans/universal_development_verification_engine_20261028/verification.md) records commands, outcomes and remaining host limitations. A blocked or unrun acceptance check remains `not_verified`.
