# UVE · Universal Development Verification Engine

UVE runs manifest-defined checks against a specific candidate, records what happened in a probe receipt, and lets a configured evaluator decide whether that evidence satisfies a gate. It can check a command-line regression, inspect a running Android app, or verify that a repaired development server recovered.

~~~mermaid
flowchart LR
    A[Profile and candidate] --> B[Probe and adapter]
    B --> C[Receipt with input identity and result]
    C --> D[Independent gate]
    D --> E[Approved, rejected, or not verified]
    C --> F[Diagnostics and optional training export]
~~~

## Install and run the demo

Clone the repository with a GitHub account that has access and open the checkout:

~~~sh
git clone https://github.com/Zelaox/uve.git
cd uve
~~~

From the repository root with Python 3.11 or newer:

~~~sh
python -B -m examples.command_regression
~~~

The demo uses the real command adapter against three isolated copies of a toy app. A pre-existing behavior check passes on the original, fails after a one-line logic change, and passes after restoration. It prints the probe IDs so you can inspect the generated JSON under `logs/uve/probes/`. It needs no Android SDK, Node.js, credentials, or network access. A project gate requires an independently assigned evaluator. See the [command example](examples/README.md) for the probe and gate commands.

The [installation and agent setup guide](docs/setup.md) shows how to bind a real project, install the [Codex skill](.agents/skills/uve-verification/SKILL.md), and use the optional Node workflow and error lookup helpers.

**Adopting UVE in your own repository:** follow the phased checklist in [`docs/plans/project-adoption/plan.MD`](docs/plans/project-adoption/plan.MD). Copy the templates under [`examples/project/`](examples/project/) (`application.contract.example.json`, `agent-app-workflow.manifest.example.json`, `uve-error-registry.manifest.example.json`) and replace placeholders — no dependency on a specific product name or device.

## What it can do

| Use case | Evidence |
| --- | --- |
| Catch a build or behavior regression | Run a declared command, record its exit and output, and bind the result to the watched files and configuration. |
| Verify Android runtime and UI | Check ADB transport, boot, foreground package, and app-specific selectors. Assert outcomes after interactions. UI fault policy can reject a visible error overlay even when a healthy selector is also present. |
| Diagnose and recover a dev server | Detect Metro health failures, lease the endpoint, and run an explicitly allowed recovery with a passing retest chain. |
| Evaluate a repair | Require the full probe set, recheck identity and receipt hashes, and reject approval by a recorded repair author. |
| Preserve evidence for later use | Compare regression baselines, link issues to receipts, or generate a training row with configured secret placeholders. |

The [case studies](docs/case-studies.md) summarize past runs with Emulator Workstation, an Expo app, Metro recovery, and an AI Lab checkpoint. The [engine guide](docs/engine.md) covers configuration, verification tiers, and integration boundaries.

## Run the tests and witness

The engine uses only the Python standard library. The suite requires pytest, Node.js for the JavaScript mutation fixture, and loopback access for the HTTP recovery fixture. Run from the checkout so the CLI can resolve `config/uve/`.

~~~sh
python -m pip install -e ".[dev]"
python -B -m agent_training.uve.cli probe run --profile config/uve/profiles/emulator_workstation_smoke.json --probe engine_self_check
python -B -m pytest -q
python -B -m agent_training.uve.cli witness run
~~~

The witness copies candidate code and configuration into a separate directory and runs contract tests there. On a managed host, use the trusted witness checkout with `witness run --candidate-root <candidate-checkout>`. The included local witness exercises the engine contract. Application acceptance requires evidence from that application's own profile and target.

This repository contains the standalone Python engine and its adapters. Emulator Workstation, the Android application, and the AI Lab runner are separate projects. The examples use synthetic inputs. Historical case studies contain summaries of local receipts with deployment details removed. Generated logs and evidence stay in ignored local directories.
