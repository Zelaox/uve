---
name: uve-verification
description: Run UVE probes and inspect receipts when a development task requires UVE verification, an existing UVE profile, or diagnosis of a failed UVE gate.
---

# UVE verification

Locate the UVE checkout and the target project's profile from the project instructions or the user's configuration. Read the profile, its command or runtime targets, and the gate's required probe list. Resolve the interpreter, workspace, and any live device or delivery bindings before running checks. For Workstation-managed apps, use the active worker's pipeline and generated application profile.

- Define observable acceptance for the change. Ensure the profile watches the affected source, tests, dependencies, and configuration. If the profile lacks a relevant check, use a declared command or application-specific UI flow with an explicit result assertion.
- Run the required probes against the final candidate using `python -B -m agent_training.uve.cli probe run --profile PROFILE --probe PROBE --repair-actor-ref ACTOR` from the UVE checkout. Record the actual implementing actor and keep the probe IDs. The optional Node workflow helper runs the same CLI.
- For failed or unavailable checks, inspect the receipt's verdict, fault origin, failure class, and observation. If the project has a registry, look up the failure using its observed fields. Preserve evidence and rerun the affected probes after changes.
- Give the required receipts to the host-assigned independent evaluator. Use the current manifest's required set and keep the resulting trust-chain reference. Report `not_verified` when a required check, binding, or evaluator is unavailable.

Distinguish engine self-checks, runtime tiers, and application acceptance. For Android UI changes, verify the intended app state after interaction. A responding dev server, a screenshot, or an approved infrastructure gate supplies evidence only for its declared checks.

When binding a new repository, read the UVE checkout's [project adoption plan](../../../docs/plans/project-adoption/plan.MD) and copy manifests from `examples/project/*.example.json` into the target app's `config/uve/`. User-flow bugs need flow receipts and invariants, not code-probe pass alone.
