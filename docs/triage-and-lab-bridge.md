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

This checkout ships the standalone validator, bundle reducer, manifests and TRIAGE index as well as the profile and gate. Set `UVE_AI_LAB_ROOT` to this checkout to use its configured report catalog and pipeline binding, or to an external AI Lab checkout with its own bindings and source reports. The two roots need not share runtime data.

```powershell
python -B scripts/debug/triage_incident_bundle.py --probe-run-id ACTUAL_PROBE_ID --write-bundle
```

Use the returned bundle path with the environment key declared in `config/lab_uve_uve_binding.json`. The native validator checks its implemented structure/mode/repair contract; approval is not a semantic proof of the incident's root cause. The standalone reducer catalog includes turn correlation and incident bundles. Dashboard/codegen/native-product reducers require the AI Lab or application checkout that owns them.

For history queries, ingestion and comparisons, see [TRIAGE observability](triage-observability.md). For binary and live forensic investigations, use the [unified skill](../.agents/skills/lab-uve-triage-bridge/SKILL.md) and its mode-specific references. Retain raw acquisitions and unsupported capabilities as explicit gaps.

Related rule metadata: `config/manifests/triage_rules_manifest.json` (`lab_incident_bundle` category for training-pipeline observability in AI Lab).

## Engine and editor contract profiles

Manifest-only examples for bound monorepos (no application data in this repository):

| Profile | Env anchor | Gate |
| --- | --- | --- |
| `config/uve/profiles/ailab_engine_development.json` | `UVE_AI_LAB_ROOT` + optional `UVE_ENGINE_ROOT` | `ailab_engine_contract_gate` |
| `config/uve/profiles/ruined_catacombs_development.json` | `UVE_AI_LAB_ROOT` + catacombs root via manifest | `ruined_catacombs_contract_gate` |

Each profile delegates to a project-owned `command_check` manifest under `config/uve/targets/<project>/`.
