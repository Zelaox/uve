# Engine guide

## Inputs and decisions

- A **profile** declares the workspace, watched and ignored paths, probe targets, execution binding, and resource leases. `uve plan` can compare inventories and select relevant probes for added, changed, and deleted files.
- A **probe** invokes an adapter and records its observation, fault classification, policy hashes, and candidate identity. When configured, required snapshots preserve the tested input bytes and verify them after execution.
- A **gate** combines the required receipts, rechecks policy and workspace freshness, and enforces repair-author separation. A candidate change calls for fresh probes against the final bytes.

| CLI exit | Probe | Gate |
| --- | --- | --- |
| `0` | `passed` | `approved` |
| `1` | `failed` | `rejected` |
| `2` | `not_verified` | `not_verified` |

## Workstation and application verification

The shipped [runtime policy](../config/uve/uve_workstation_runtime.json) declares these tiers for the Workstation worker:

| Tier | Required probes | Gate |
| --- | --- | --- |
| V1 | `workstation_structure` | `emulator_runtime_v1` |
| V2 | `workstation_transport` | `emulator_runtime_v2` |
| V3 | `adb_boot_smoke` and `adb_ui_flow` | `emulator_runtime_v3` |

The worker receives a selected SDK, serial, and package from its host. It materializes probe targets from that context and leaves unavailable bindings `not_verified`. `uve diagnose` compares stored runtime status and receipts. Add `--context` with the matching context file to check its identity against status.

Application acceptance has a separate contract at `config/uve/application.json` in the bound project. The default workstation policy requires `application_code`, `application_bundle`, and `application_health`. Contracts may optionally bind feature UI flows (for example via `membership_ui_flow.generatedFlowRelativePath`), which materialize as additional probes such as `application_membership_ui_flow` when the generated flow file exists. Define gates and runtime `probe_ids` to match each project's contract.

The [Android example profile](../config/uve/profiles/android_example_development.json) shows how to wire a local checkout, an ADB target, a Metro endpoint, and a UI flow. The shipped ADB serial is blank. For a live run, copy the target and profile, set the workspace through `UVE_ANDROID_EXAMPLE_ROOT`, and bind the actual ADB executable, selected serial, foreground package, and expected UI state. The sample flow checks boot and a foreground package. Add app-specific selectors and state assertions for feature acceptance. Screenshots are supplementary evidence.

`adb_ui_flow` steps may include `navigation_recovery` before feature taps. The step loads a project-owned policy manifest (for example `config/uve/targets/<project>/navigation_recovery_main_tabs.json`) and runs the `ui_navigation_recovery` adapter to relaunch the guest package, dismiss blocking overlays, and return to a known tab/stack without blind Back spam. Failures classify as `navigation_recovery_exhausted`; UIAutomator dump failures classify as `uiautomator_unavailable` in the engine failure-class catalog. See [docs/UVE.md](UVE.md) for Workstation integration and application acceptance boundaries.

The AI Lab adapter invokes a separately installed runner through `UVE_AI_LAB_ROOT` and a run manifest. The historical-import path accepts a `training_quality_receipt.v1` document. The checkpoint gate requires the live runner as its evidence source.

## Evidence handling

Probe and gate JSON, artifacts, snapshots, and logs are written under `logs/uve/`. These can include private paths, command output, or screenshots. Keep that store private. Export applies the configured placeholder rules to training rows. Review exports before sharing them because the rules cover specific patterns.

Use protected policy and witness checkouts with host-assigned roles. Run untrusted commands and candidate code under a separate OS or sandbox boundary. A role string identifies a caller for policy checks, and a snapshot isolates watched inputs. Process and filesystem permissions come from the host.
