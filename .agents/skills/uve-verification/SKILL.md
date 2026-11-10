---
name: uve-verification
description: Run UVE probes and inspect receipts when a development task requires UVE verification, an existing UVE profile, or diagnosis of a failed UVE gate.
---

# UVE verification

Locate the UVE checkout and the target project's profile from the project instructions or the user's configuration. Read the profile, its command or runtime targets, and the gate's required probe list. Resolve the interpreter, workspace, and any live device or delivery bindings before running checks. For Workstation-managed apps, use the active worker's pipeline and generated application profile.

- Define observable acceptance for the change. Ensure the profile watches the affected source, tests, dependencies, and configuration. If the profile lacks a relevant check, use a declared command or application-specific UI flow with an explicit result assertion.
- Run the required probes against the final candidate using `python -B -m agent_training.uve.cli probe run --profile PROFILE --probe PROBE --repair-actor-ref ACTOR` from the UVE checkout. Record the actual implementing actor and keep the probe IDs. The optional Node workflow helper runs the same CLI.
- For failed or unavailable checks, inspect the receipt's verdict, fault origin, failure class, and observation. For `deep_flow` receipts, run `python -B -m agent_training.uve.cli flow triage --probe-run-id ID` before editing adapters. If the project has a registry, look up the failure using its observed fields. Preserve evidence and rerun the affected probes after changes.
- When AI Lab triage bundles are in scope, use profile `config/uve/profiles/lab_incident_bundle_validate.json` with `UVE_AI_LAB_ROOT` bound to the Lab checkout. Do not treat bundle validation as engine self-check.
- Give the required receipts to the host-assigned independent evaluator. Use the current manifest's required set and keep the resulting trust-chain reference. Report `not_verified` when a required check, binding, or evaluator is unavailable.

Distinguish engine self-checks, runtime tiers, and application acceptance. For Android UI changes, verify the intended app state after interaction. A responding dev server, a screenshot, or an approved infrastructure gate supplies evidence only for its declared checks.
