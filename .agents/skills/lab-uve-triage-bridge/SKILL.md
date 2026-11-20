---
name: lab-uve-triage-bridge
description: Diagnose with TRIAGE, reuse existing reverse-engineering evidence, and verify scoped results through UVE. Use for incident bundles, indexed history, binary investigations, or forensic evidence; application acceptance stays with the target project's contract.
---

# UVE, TRIAGE and reverse engineering

TRIAGE diagnoses. Analysis tools produce observations. UVE records probes and evaluates declared gates. Keep their scopes distinct: a valid bundle, intact acquisition, or passing parser check does not establish a repaired application or healthy storage device.

## Bind the actual system

Resolve the UVE checkout, interpreter, target workspace, profile, receipt store and actual implementing actor from project configuration. Read [the engine guide](../../../docs/UVE.md) and the current profile, probe catalog, gate requirements and execution-binding policy. For Workstation-managed apps use the active worker's pipeline and generated application profile; do not mix source, build and distribution receipts.

For an external AI Lab checkout, read its `config/lab_uve_uve_binding.json` and `config/uve_triage_storage_manifest.json`. This standalone repository binds its own pipeline to `.`. App/dashboard reducers and native product contracts remain owned by those projects.

## Start with indexed history

Use the configured read-only TRIAGE API, or run from the bound checkout:

```powershell
python -B -m mcp_server.diagnostics.uve_triage_query project-health --project-id PROJECT
python -B -m mcp_server.diagnostics.uve_triage_query run-summary --run-id RUN
python -B -m mcp_server.diagnostics.uve_triage_query trace-finding --fingerprint FINGERPRINT
```

Use `fingerprint-timeline --fp` and `comparison-show --comparison-id` for recurrence and saved comparisons. Follow evidence references to original reports and receipts, checking hashes, candidate identity and freshness. A conservative index summary is not a replacement for the original receipt and current gate validation. Missing or partial coverage is a gap, not proof that no earlier incident exists. See [TRIAGE observability](../../../docs/triage-observability.md).

## Select the investigation mode

- For binary interpretation, decoding or parser work, first read [reverse engineering and Learning DB reuse](references/reverse-engineering.md). Reuse matching prior measurements before new experiments.
- For a live machine, disk, saved logs or crash dumps, read [forensic acquisition and verification scope](references/forensics.md). Preserve acquisition bytes and record what was not acquired.
- For incident bundles, use `scripts/debug/triage_incident_bundle.py` with actual run/turn/probe identifiers. Reports belong only under the configured report catalog. The reducer must not repair sources, modify work runs or edit existing receipts.

## Prove the actual assertion

Define observable acceptance and ensure the profile watches all relevant code, policies and acquisition inputs. Where an existing profile has insufficient coverage, prepare a case-scoped profile and command target through the installed schema; do not weaken trusted policy or omit large inputs silently.

Run real probes on stable candidate bytes, with the actual implementing actor. Inspect verdicts, observations, identities, hashes, execution bindings and failure coordinates. Use the host-assigned evaluator for the gate; an alternate name for the author does not establish independence. Keep failed and `not_verified` receipts. Re-run affected probes after candidate, target, manifest or verifier changes.

For failures, use the target project's implemented registry with the receipt's actual failure class/rule; for gate rejections use `rejection_basis`. A registry match is advisory. Missing bindings, unavailable evaluators, unsupported adapters and incomplete acquisition remain `not_verified`.

Application work also needs its declared behavior checks. V1/V2/V3 runtime infrastructure and a bundle gate cannot approve an application's business logic. Static command checks do not acquire emulator requirements merely because they use UVE.

Deliver proven observations, hypotheses, unresolved coverage, source/candidate hashes, commands, probe IDs and gate receipt references. State the precise capability that remains `not_verified`; do not generalize a narrow approval.
