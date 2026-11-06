# Install UVE and use it with a coding agent

## Start from a checkout

Use Git and Python 3.11 or newer. Clone the private repository with an account that has access.

~~~sh
git clone https://github.com/Zelaox/uve.git
cd uve
python -B -m examples.command_regression
~~~

Expect `passed`, `failed`, and `passed`. The failure is the intentional regression. Each probe prints an ID you can use to find its receipt in `logs/uve/probes/`. The engine has no runtime package dependencies. Use `python3` if that is the name of your Python command.

To install the shorter `uve` command in your current Python environment, run `python -m pip install -e .` from the checkout. Use an activated virtual environment if you want an isolated installation. Keep the checkout in place because it supplies the manifests. The commands below use the Python module directly and also work without editable installation.

## Connect a project

The [sample profile](../examples/project/profile.json) binds to a separate workspace through `UVE_PROJECT_ROOT`, watches Python sources and tests, and runs the declared [command](../examples/project/command.json). Try it first with the included fixture. Run these PowerShell commands from the UVE checkout:

~~~powershell
$env:UVE_PROJECT_ROOT = (Resolve-Path 'examples/project/fixture').Path
$uveProfile = 'examples/project/profile.json'
$uveActor = 'ACTUAL_IMPLEMENTING_ACTOR'
python -B -m agent_training.uve.cli probe run --profile config/uve/profiles/emulator_workstation_smoke.json --probe engine_self_check
python -B -m agent_training.uve.cli probe run --profile "$uveProfile" --probe command_check --repair-actor-ref "$uveActor"
~~~

In a POSIX shell, use these assignments and then run the same two Python commands:

~~~sh
export UVE_PROJECT_ROOT="$PWD/examples/project/fixture"
uveProfile='examples/project/profile.json'
uveActor='ACTUAL_IMPLEMENTING_ACTOR'
~~~

Replace the actor placeholder with the actual person or agent doing the work.

For your own project:

1. Copy `profile.json`, `root.json`, and `command.json` from `examples/project/` into a project-specific directory in the UVE checkout. Update the profile's references to point to the copies. UVE resolves relative manifest references from its checkout root. Absolute paths can bind manifests stored in a separate repository.
2. Set `UVE_PROJECT_ROOT` to the actual project path. Choose a unique `project_id`. Update the watch globs to cover the source, tests, and relevant dependency and build files. Keep generated outputs outside the watched set.
3. Replace the command's `argv` with the actual build or test command as separate arguments. UVE executes it in the bound project without a shell. Keep `{python}` for Python commands, or use the tool installed for your project. Install the project's own dependencies in the execution environment.
4. Run the probe and save its `probe_run_id`. After changes, rerun it against the final files. A separately assigned evaluator runs the gate with the matching IDs.

~~~powershell
python -B -m agent_training.uve.cli gate evaluate --profile "$uveProfile" --gate command_check --probe-run-id PROBE_RUN_ID --evaluator-ref ASSIGNED_EVALUATOR --repair-actor-ref "$uveActor"
~~~

Use the same UVE checkout, workspace binding, and profile as the probe. Replace `PROBE_RUN_ID` and `ASSIGNED_EVALUATOR` with actual values. If a gate requires more than one probe, repeat `--probe-run-id` for each receipt. Keep the inputs stable through gate evaluation. The host assigns and enforces evaluator identity. When the evaluator or required evidence is unavailable, record `not_verified`.

For a Workstation-managed app, use the installed worker's actual pipeline and selected application profile. The [engine guide](engine.md) explains how Workstation runtime tiers, app acceptance, and Android bindings fit together.

## Connect Codex or another coding agent

Copy the [uve-verification skill](../.agents/skills/uve-verification/SKILL.md) into the **target project's** `.agents/skills/uve-verification/` folder. For example, from the UVE checkout in PowerShell, with `$uveTarget` set to the target project path:

~~~powershell
$uveSkillDir = Join-Path $uveTarget '.agents/skills/uve-verification'
New-Item -ItemType Directory -Path $uveSkillDir -Force | Out-Null
Copy-Item -LiteralPath '.agents/skills/uve-verification/SKILL.md' -Destination $uveSkillDir
~~~

Open the target project in Codex. In the CLI or IDE extension, invoke `$uve-verification`. You can also ask Codex to read the skill by path. If an existing project has a skill of the same name, review its instructions before replacing it. See the [official Codex skill docs](https://learn.chatgpt.com/docs/build-skills).

Put the checkout path, profile reference, and gate ID in the target project's `AGENTS.md`. For another agent, put the same instructions in its project rules or give it the skill file and the following prompt. The agent needs shell access to the UVE checkout and the target workspace. Live checks also need access to the selected device or service.

> Use UVE for this change. Run the Python CLI from UVE checkout UVE_CHECKOUT. Use profile UVE_PROFILE and the required probes for gate UVE_GATE. Check that the profile watches the files this change affects. Record your actual repair actor, run the probes on the final files, and report the probe IDs, verdicts, and any failure cause. Pass the receipts to the assigned evaluator. Report the gate as not_verified if its evidence or evaluator is missing.

## Optional Node helpers

The helpers use Node.js built-in modules and were tested with Node.js 22. From the UVE checkout:

~~~powershell
node scripts/uve-app-workflow.mjs --profile $uveProfile --repair-actor-ref $uveActor
node scripts/uve-error-registry-lookup.mjs --from-receipt FAILED_PROBE_RUN_ID
~~~

- [`uve-app-workflow.mjs`](../scripts/uve-app-workflow.mjs) runs the profile's `verification_probe_ids` in order and stops on the first non-passing probe. Set `UVE_PYTHON_EXECUTABLE` if your Python executable differs from `python`. Supply repeatable `--probe` flags for a generated profile that lacks the probe list. Read the actual gate manifest to choose the required probes. The helper records probes and leaves gate evaluation to the assigned evaluator.
- [`uve-error-registry-lookup.mjs`](../scripts/uve-error-registry-lookup.mjs) reads a probe receipt and matches observed fields against the [sample registry](../examples/project/error-registry.json). Pass `--registry PATH` for a project registry in the same format, or query observed fields with `--probe-id`, `--failure-class`, and `--fault-rule`. A match exits `0`, no match exits `2`, and invalid input exits `1`. Lookup provides diagnostic guidance. The receipt still determines the probe result.

The portable helpers use the checked-out engine and sample registry. Existing app-specific wrappers and registries belong with the projects that own their contracts, selectors, and recovery policies.

## Full adoption checklist (any project)

For command-only projects through Android application acceptance, agent workflows, error registries, and user-flow invariants, use the product-neutral plan:

- [`docs/plans/project-adoption/plan.MD`](plans/project-adoption/plan.MD)

Templates to copy into **your** app repository (edit every `YOUR_*` placeholder):

| File in this checkout | Purpose |
| --- | --- |
| [`examples/project/application.contract.example.json`](../examples/project/application.contract.example.json) | `application.json` shape (code, bundle, health, feature flow binding) |
| [`examples/project/agent-app-workflow.manifest.example.json`](../examples/project/agent-app-workflow.manifest.example.json) | Probe order, debug hints, optional flow manifest paths |
| [`examples/project/uve-error-registry.manifest.example.json`](../examples/project/uve-error-registry.manifest.example.json) | Resolution entries and gate rejection hints |
| [`examples/project/error-registry.json`](../examples/project/error-registry.json) | Minimal registry for the command demo only |
