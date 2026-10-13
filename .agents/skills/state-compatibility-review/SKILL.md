---
name: state-compatibility-review
description: Use when a task changes or reviews persisted state, schemas, migrations, data-root behavior, account metadata, locks, journal recovery, or restart compatibility. Skip ordinary docs and read-only changes with no persisted-state impact.
---

# State compatibility review

Use this skill only after confirming the current task handoff and owned files.

1. Read the applicable rules in [AGENTS.md](../../../AGENTS.md) and use [CONTEXT_MAP.md](../../../docs/CONTEXT_MAP.md) to select the relevant source, tests, and contract documents.
2. Trace the current read and write paths in source and tests. Identify each persisted namespace, its owner, and its schema version. Treat checkpoint documents as historical evidence unless the current issue confirms their applicability.
3. Preserve THREADS_LOCAL_DATA_ROOT, account UUID and alias metadata, env:// credential references, and existing v1 store formats. Do not silently migrate or rewrite v1 state. A new persisted format needs a separately reviewed, versioned contract and explicit file ownership.
4. Check strict shape validation, path containment, account-scoped locking, atomic updates, dedupe, and behavior after process restart. Keep NurtureStore v1 and LocalOperationStore v1 as the existing state and effect authorities.
5. Treat AMBIGUOUS outcomes as a stop condition: no automatic retry or resend across errors or restarts. Preserve corrupt or unreadable state for review. Nurture fingerprints support selection and dedupe; they do not recover remote IDs.
6. Use only fresh temporary synthetic roots for tests or demos. Never inspect, copy, migrate, or test against operator or customer roots, profiles, credentials, or journals.

Stop if the task lacks state ownership, a schema contract, a safe recovery path, or a required authorization. Report the specific conflict and a minimal contract proposal.

## Read next

- [State compatibility rehearsal](../../../docs/STATE_COMPATIBILITY_REHEARSAL.md)
- [Nurture parity](../../../docs/NURTURE_PARITY.md)
- [Mutation parity](../../../docs/MUTATION_PARITY.md)
- [Accounts](../../../src/threads_platform/standalone/accounts.py)
- [Nurture store](../../../src/threads_platform/standalone/nurture_store.py)
- [Mutation runtime](../../../src/threads_platform/standalone/mutations.py)
- [State compatibility tests](../../../tests/test_state_compatibility.py)
- [Nurture durability tests](../../../tests/test_nurture_durability.py)