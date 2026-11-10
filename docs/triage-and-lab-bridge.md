# Flow triage and AI Lab incident bridge

UVE records immutable probe receipts. Two complementary diagnostics layers sit on top of that evidence:

1. **Flow triage** — inspect `deep_flow` and composite probe receipts without re-running adapters.
2. **Lab incident bundle gate** — optional `command_check` profile that validates an AI Lab triage bundle when `UVE_AI_LAB_ROOT` is bound.

## Flow triage (receipt-local)

After a probe run, use the flow subcommands against the stored receipt:

~~~powershell
python -B -m agent_training.uve.cli flow status
python -B -m agent_training.uve.cli flow triage --probe-run-id PROBE_RUN_ID
python -B -m agent_training.uve.cli flow replay --probe-run-id PROBE_RUN_ID
python -B -m agent_training.uve.cli flow compare --probe-run-id FIRST --with-probe-run-id SECOND
~~~

Triage projects observation fields through `config/uve/uve_deep_flow_manifest.json` and returns a primary finding fingerprint. Failed deep-flow probes can open deduplicated issues through `uve_issue_bridge_manifest.json` using flow fingerprint leases.

Contract coverage lives in `tests/test_uve_deep_flow.py` and is part of the witness contract set.

## AI Lab incident bundle profile (external workspace)

The profile `config/uve/profiles/lab_incident_bundle_validate.json` watches AI Lab bridge manifests and runs:

~~~text
python scripts/debug/validate_lab_uve_incident_bundle.py
~~~

inside the project bound by `UVE_AI_LAB_ROOT`. The probe succeeds when the validator prints `BUNDLE_VALID`. Gate id: `lab_incident_bundle_gate`.

This checkout ships the profile and gate definitions only. The validator script and live bundle bytes live in the AI Lab repository, not here. Set `UVE_AI_LAB_ROOT` to that checkout before running the probe.

Related rule metadata: `config/manifests/triage_rules_manifest.json` (`lab_incident_bundle` category for training-pipeline observability in AI Lab).

## Engine and editor contract profiles

Manifest-only examples for bound monorepos (no application data in this repository):

| Profile | Env anchor | Gate |
| --- | --- | --- |
| `config/uve/profiles/ailab_engine_development.json` | `UVE_AI_LAB_ROOT` + optional `UVE_ENGINE_ROOT` | `ailab_engine_contract_gate` |
| `config/uve/profiles/ruined_catacombs_development.json` | `UVE_AI_LAB_ROOT` + catacombs root via manifest | `ruined_catacombs_contract_gate` |

Each profile delegates to a project-owned `command_check` manifest under `config/uve/targets/<project>/`.
