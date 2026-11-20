# Reverse engineering with existing knowledge

The Hex Editor is an external analysis application. This repository supplies the UVE/TRIAGE integration and read-only lookup helper, not the editor's parser/decoder implementation or a user's Learning DB.

Resolve the bound Lab's `config/hex_editor.json`, its configured `application_path`, workspace allowlist, and the editor's `config/learning_db_policy.json` / `learning_db_registry.json`. Read the tool CLI's actual `list` and `describe` contracts before using operations. A configured path does not itself authorize access.

## Reuse before new analysis

After minimal source identity/header reads, inspect the configured Learning DB through `binary_learning_db_inspect` or the editor's read-only APIs. The legacy migration/sync readers are not read-only substitutes.

Use this skill's bounded lookup helper for exact source, parent, candidate-output and parser-input identities:

```powershell
python -B PATH_TO_SKILL/scripts/lookup_hex_learning.py --lab-root LAB_ROOT --identity-sha256 SOURCE_SHA256
python -B PATH_TO_SKILL/scripts/lookup_hex_learning.py --lab-root LAB_ROOT --filter format_plugin=OBSERVED_FORMAT
```

Filters are exact matches on declared policy columns; they are not fuzzy search. Select tables and limits only within the configured policy. Preserve truncation, schema gaps and missing tables. A nonempty WAL produces `requires_checkpointed_snapshot`: use an owner-produced consistent snapshot, never discard a WAL or checkpoint another process's live store.

Inspect matching runs, candidates, findings, parser observations, proof spans, cross-run deltas and preservation events. Follow IDs to original artifacts and validate current hashes, lineage, byte mappings, policy and producer versions. A database flag is an index hint, not UVE approval. Record failed or incompatible attempts so unchanged dead ends are not retried without reason.

Summarize reuse as exact compatible evidence, related hypotheses, incompatible matches, or unavailable/incomplete coverage. Then select the smallest new measurement for unresolved questions. An unavailable index does not stop unrelated authorized analysis or establish that a format is new.

## Analysis and proof

Preserve immutable input copies and acquisition provenance. Keep offsets and lengths with observations, and distinguish file offset, RVA/VA, decoded offsets and semantic positions. Do not transfer locations between them without a real mapping.

Use enabled JSON CLI tools for bounded reads, format inspection, parser snapshots, transforms or forensic saved-copy analysis. Run the matching inspect/verify contracts after artifact generation. Routing metadata such as `evidence_read` does not guarantee that an implementation writes no cache or derived artifact; inspect its declared effects and use authorized output roots.

Do not fabricate parent candidates, keys, scopes, source maps or verification flags. A filename, readable string, reversible transform, dispatch success or `ok: true` is not proof of a correct interpretation. Retain hypotheses, alternatives, falsification checks, negative results and unresolved coverage.

Bind actual source/candidate bytes, acquisition/specification, policies, parser/verifier implementation and relevant checks to a scoped UVE profile. A deterministic command must parse tool results and assert the claimed invariants; merely running an information command is insufficient. Use the real implementing actor and configured independent gate. Tests of fixtures establish only their covered behavior; a claim about an actual file also needs that file's identity and assertions.

Deliver verified spans and preserved/unresolved spans separately. If an adapter, identity, evaluator or required assertion is missing, state `analysis completed; UVE not_verified` for that capability.
