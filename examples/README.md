# Command regression example

Run `python -B -m examples.command_regression` from the repository root. The script materializes a profile and command for each candidate under `logs/uve/examples/` and runs `command_check` using UVE's real adapter. The three candidates remain available with their receipts.

The command manifest declares `argv`, `timeout_seconds`, `expected_returncode`, and `expected_stdout`. The profile points to it via `command_probe_refs.command_check`. The adapter runs the command in the resolved workspace without a shell. The profile watches the toy app and the behavior test.

For your own candidate, run a probe as the implementing actor, using the actual profile path and actor reference:

~~~sh
python -B -m agent_training.uve.cli probe run --profile YOUR_PROFILE --probe command_check --repair-actor-ref YOUR_ACTOR
~~~

A separately assigned evaluator can check the resulting `probe_run_id` against the `command_check` gate. Replace the placeholders with the actual probe and actor references:

~~~sh
python -B -m agent_training.uve.cli gate evaluate --profile YOUR_PROFILE --gate command_check --probe-run-id PROBE_RUN_ID --evaluator-ref ASSIGNED_EVALUATOR --repair-actor-ref YOUR_ACTOR
~~~

For a gate with several required probes, repeat `--probe-run-id` for each receipt from the same stable candidate.

## Templates for a real application

After the command demo, copy and adapt the examples in [`examples/project/`](project/):

- `profile.json`, `root.json`, `command.json` — minimal command profile (Phase 1 in the [adoption plan](../docs/plans/project-adoption/plan.MD))
- `application.contract.example.json` — application acceptance contract for your app repo
- `agent-app-workflow.manifest.example.json` — agent probe sequence and debug map
- `uve-error-registry.manifest.example.json` — failure resolution catalog

The adoption plan walks through Workstation binding, user flows, and invariant severity without assuming a particular product.
