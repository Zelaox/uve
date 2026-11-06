# UVE · Universal Development Verification Engine

UVE runs **manifest-defined probes** against a specific candidate, writes immutable **probe receipts** (identity, observation, failure class), and lets a configured **independent evaluator** approve or reject a **gate**. It is built for real development workflows: command regressions, Android/Metro runtime checks, application acceptance, dev-server recovery, and evidence-bound diagnostics—not ad-hoc smoke scripts.

~~~mermaid
flowchart LR
    P[Profile + watched candidate] --> R[Probe via adapter]
    R --> C[Receipt]
    C --> G[Gate evaluate]
    G --> D[approved / rejected / not_verified]
    C --> X[plan · diagnose · witness · export]
~~~

| Decision | Probe verdict | Gate decision | CLI exit |
| --- | --- | --- | --- |
| Success | `passed` | `approved` | `0` |
| Subject fault | `failed` | `rejected` | `1` |
| Missing binding / evaluator | `not_verified` | `not_verified` | `2` |

**Rules that do not bend:** fresh probes after candidate or manifest changes; repair author ≠ gate evaluator; receipts beat screenshots and log snippets; `not_verified` is an honest outcome, not a pass.

---

## Quick start

Clone with a GitHub account that has access:

~~~sh
git clone https://github.com/Zelaox/uve.git
cd uve
python -B -m examples.command_regression
~~~

That demo runs the real **command** adapter on three isolated toy trees: pass → intentional one-line break → restore. Probe run IDs are printed; receipts live under `logs/uve/probes/`. No Android SDK, Node, or network required.

Install for development and run the contract suite:

~~~sh
python -m pip install -e ".[dev]"
python -B -m agent_training.uve.cli probe run --profile config/uve/profiles/emulator_workstation_smoke.json --probe engine_self_check
python -B -m pytest -q
python -B -m agent_training.uve.cli witness run
~~~

Optional entry point after editable install: `uve` (same as `python -B -m agent_training.uve.cli`).

---

## What the engine provides

### Core model

- **Profiles** — workspace root, watch/ignore globs, probe refs, execution binding, resource **leases**.
- **Probes** — declared in `config/uve/uve_probe_catalog.json`; implemented by **adapters** under `agent_training/uve/adapters/`.
- **Gates** — required probe set, identity stability, repair-author separation (`config/uve/uve_gate_manifest.json`).
- **Evidence** — JSON receipts, optional snapshots, content-addressed artifacts (e.g. UI PNGs), gates under `logs/uve/`.

### Verification tiers (Emulator Workstation worker)

When embedded in a host that ships `uve_workstation_runtime.json`, the worker exposes infrastructure tiers separate from **application** acceptance:

| Tier | Probes (shipped policy) | Gate |
| --- | --- | --- |
| **V1** | `workstation_structure` | `emulator_runtime_v1` |
| **V2** | `workstation_transport` | `emulator_runtime_v2` |
| **V3** | `adb_boot_smoke`, `adb_ui_flow` | `emulator_runtime_v3` |

V1–V3 prove engine/transport/guest infrastructure. They do **not** prove your app’s business logic or full UI flows.

### Application acceptance (project-owned contract)

A bound application repo may declare `config/uve/application.json`. The host worker materializes a generated profile and runs probes such as:

| Probe | Role |
| --- | --- |
| `application_code` | Declared static/scenario commands in the workspace |
| `application_bundle` | Live bundle/dev-server compile checks |
| `application_health` | Foreground UI + bounded logcat; **fault policy** rejects ErrorBoundary/LogBox overlays even when a “healthy” selector is visible |
| `application_membership_ui_flow` | Optional feature UI flow when the contract binds a generated flow file |

Default shipped **application** gate requires code, bundle, and health only. Extra feature probes and gate requirements are defined per project contract and host policy. Application contracts and app-specific UI flows stay in **your application repository**, not in this engine repo.

### Android UI flows and navigation recovery

`adb_ui_flow` steps include taps, asserts, shell/window checks, activities, and optional **`navigation_recovery`**: relaunch guest package, dismiss blocking overlays, return to a known tab/stack via a project-owned policy JSON (under `config/uve/targets/<project_id>/` in the app or copied example targets).

Failure classes include `navigation_recovery_exhausted` and `uiautomator_unavailable` (see `config/uve/uve_failure_class_catalog.json`). UI flows are guarded by `config/uve/application_fault_policy.json`.

Sample wiring: [`config/uve/profiles/android_example_development.json`](config/uve/profiles/android_example_development.json) and targets under [`config/uve/targets/android_example/`](config/uve/targets/android_example/). Set `UVE_ANDROID_EXAMPLE_ROOT` to your checkout for inventory and command probes.

### Metro, leases, and recovery

- **Metro** — `metro_status` / Expo adapter; profile-scoped port leases.
- **Recovery** — `recovery` CLI action with manifest allow-list (`config/uve/uve_recovery_manifest.json`); requires reverification probe chain (e.g. `metro_status` after `restart_metro`).
- **Leases** — parallel holders blocked with `lease_denied`; serial, port, and other resources via `uve_resource_lock_manifest.json`.

### Planning, regression, and diagnostics

- **`plan`** — compare file inventories; select probes for added/changed/deleted paths.
- **`regression capture|compare`** — baseline gate receipts for drift detection.
- **`diagnose`** — read stored status/receipts and context hash consistency **without** running ADB or re-evaluating gates (Workstation log `verification/context.json` + pipeline `logs/uve/`).
- **`status`** — engine degraded flag and runtime summary.
- **`witness run`** — copy candidate into an isolated tree and run contract tests (host-trusted witness for production certification).

### Other adapters

| Adapter | Typical probes |
| --- | --- |
| `command` | `command_check`, application code/bundle, workstation transport |
| `adb` / `adb_lifecycle` | Boot smoke, UI flows, emulator launch/stop acceptance |
| `application_health` | Runtime health + fault policy + logcat bounds |
| `expo_metro` | Metro status and restart recovery |
| `process_health` | Dev server / process expectations |
| `engine_meta` | Engine self-check, workstation structure |
| `ai_lab_bridge` | External AI Lab quality tiers (optional checkout via `UVE_AI_LAB_ROOT`) |
| `dry` | Plumbing probes (`dry_echo`) |

Catalog source of truth: [`config/uve/uve_probe_catalog.json`](config/uve/uve_probe_catalog.json).

---

## CLI cheat sheet

Run from the UVE checkout root (manifests resolve relative to this tree):

~~~sh
# Probe (record repair actor)
python -B -m agent_training.uve.cli probe run \
  --profile PATH_TO_PROFILE.json \
  --probe PROBE_ID \
  --repair-actor-ref YOUR_ACTOR

# Gate (independent evaluator — not the repair author under another name)
python -B -m agent_training.uve.cli gate evaluate \
  --profile PATH_TO_PROFILE.json \
  --gate GATE_ID \
  --probe-run-id PROBE_RUN_ID \
  --evaluator-ref ASSIGNED_EVALUATOR \
  --repair-actor-ref YOUR_ACTOR

python -B -m agent_training.uve.cli plan --profile PROFILE --write-inventory before.json
python -B -m agent_training.uve.cli recovery --profile PROFILE --action restart_metro
python -B -m agent_training.uve.cli diagnose --context PATH/verification/context.json
python -B -m agent_training.uve.cli status
~~~

Repeat `--probe-run-id` for each required probe when a gate needs more than one receipt.

---

## Bind your own project

1. **Command-only (no device)** — copy and edit [`examples/project/`](examples/project/) (`profile.json`, `root.json`, `command.json`); set `UVE_PROJECT_ROOT`. See [examples/README.md](examples/README.md).
2. **Android / Metro** — copy [`android_example`](config/uve/profiles/android_example_development.json) profile and targets; bind ADB serial, package, port, and UI flow manifests.
3. **Full app acceptance** — add `application.json` in **your app repo**; host worker discovers it and materializes generated profiles under `logs/uve/runtime/.../application/`.

Agent integration: [docs/setup.md](docs/setup.md) and [`.agents/skills/uve-verification/SKILL.md`](.agents/skills/uve-verification/SKILL.md).

Optional Node helpers (Node 18+):

~~~sh
node scripts/uve-app-workflow.mjs --profile YOUR_PROFILE --repair-actor-ref YOUR_ACTOR
node scripts/uve-error-registry-lookup.mjs --from-receipt PROBE_RUN_ID --registry PATH/TO/registry.json
~~~

Project-owned error registries (resolution steps for `failure_class` / fault rules) live with the application; the lookup script is format-compatible with [`examples/project/error-registry.json`](examples/project/error-registry.json).

---

## Documentation map

| Document | Contents |
| --- | --- |
| [docs/setup.md](docs/setup.md) | Install, bind a workspace, agents, Node helpers |
| [docs/engine.md](docs/engine.md) | Profiles, tiers, application contracts, navigation recovery |
| [docs/UVE.md](docs/UVE.md) | Workstation worker, context/status, diagnose, recovery, leases (host integration) |
| [docs/case-studies.md](docs/case-studies.md) | Sanitized historical runs |
| [examples/README.md](examples/README.md) | Command regression demo |

Configuration lives under [`config/uve/`](config/uve/) (probe catalog, gates, fault policy, workstation runtime policy, expectations, example targets).

---

## Repository boundaries

| In **this** repo | Separate projects |
| --- | --- |
| Python engine, adapters, manifests, tests, witness contract | Emulator Workstation host binary and its install layout |
| Generic `android_example` targets | Your mobile/web app source and `application.json` |
| Command regression example | AI Lab runner checkout (optional adapter) |

Generated logs, probes, gates, and local engine state under `logs/uve/` are not committed. Case studies summarize receipts with deployment details removed.

---

## Requirements

- **Python 3.11+** (stdlib-only at runtime)
- **pytest**, **Node.js** (for JS mutation acceptance tests), loopback HTTP for recovery fixtures — dev/CI only
- Live Android verification requires host-bound ADB, emulator, and manifests; the engine does not ship a device or app

License and access: private repository; clone with an authorized GitHub account.
