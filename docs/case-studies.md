# UVE in use

These historical summaries are based on local probe and gate records from earlier installations. They describe those candidates and environments. The repository examples create their own evidence.

## Workstation startup and foreground verification

The installed Workstation started UVE as an owned worker, detected the selected Android guest, and approved V1 engine structure, V2 ADB transport, and V3 boot and foreground assertions. Sending the Expo app to the background kept V1 and V2 approved and caused V3 to reject. After a restart fix, the worker rediscovered the still-running guest and all three tiers approved again. A later diagnostics run found consistent stored receipts and matching context for its installed candidate.

## Behavioral fault injection in an Expo app

A pre-existing test checked which part of a display name was returned. In an isolated candidate, a one-character index change selected the wrong token. JavaScript syntax checks passed throughout. The application command probe and its gate recorded the behavioral difference:

| Candidate | Command probe | Gate |
| --- | --- | --- |
| Original | `passed` | `approved` |
| Wrong token index | `failed` with `subject_fault` | `rejected` with `subject_observed` |
| Restored | `passed` | `approved` |

The application's existing test stayed unchanged. The [runnable example](../examples/README.md) reproduces the same kind of regression with synthetic names and a Python fixture.

## Recovery and isolated Android lifecycle

- **Metro recovery.** A separate Expo server was started and deliberately stopped. Its health probe failed. A declared recovery restarted it and recorded a passing final retest while the primary Metro session stayed available.
- **Android lifecycle.** UVE leased an unused guest and transport, observed a cold boot, captured a display artifact, and verified clean process exit and transport disappearance. The lifecycle gate approved.

## AI Lab checkpoint verification

The live bridge ran the authoritative V1 checks for a checkpoint, preserved its candidate identity, and received an approved UVE checkpoint gate. A historical V1 import preserved the original checks, while the gate that required live runner evidence returned `not_verified`. These runs covered V1 checkpoint quality.
