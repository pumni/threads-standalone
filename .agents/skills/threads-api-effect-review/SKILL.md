---
name: threads-api-effect-review
description: Use when a task adds, changes, or reviews a Threads API call that can create, reply, publish, moderate, or otherwise mutate remote state. Skip read-only API work and local planning that cannot reach a remote mutation.
---

# Threads API effect review

This review does not grant Meta, platform, account, or operator permission. Confirm the current handoff and separately required authorization before treating any effect as permitted.

1. Trace the call from its CLI or application entry point to the request adapter. Confirm the exact documented official Threads API endpoint and classify the operation as a remote read or mutation; do not infer this from a method name.
2. For a mutation, verify the endpoint-specific app and token scope, account and actor binding, exact target/root/parent, and exact reviewed final content. Missing or changed bindings mean STOP. A permalink or fingerprint is not a substitute for reacquiring the permitted full target binding.
3. Keep OBSERVE_ONLY, REVIEW_QUEUE, and APPROVED_DISPATCH separate. A plan, draft, keyword match, or content provider must not silently authorize dispatch. New recipients need independent eligible context and review of the exact account, target, and final text.
4. Check that account-scoped locking, validation, dedupe/cooldown, and the existing LocalOperationStore journal cover the effect. Do not add a second effect authority or bypass the journal.
5. Verify failure behavior on both sides of the remote-effect boundary. Once an outcome may have occurred, persist AMBIGUOUS and block automatic retry or resend, including after restart. A timeout or lost response does not prove that nothing happened.
6. Report PUBLISHED, author-side API readback, and third-party visibility as distinct evidence. API acknowledgement alone does not establish public visibility.
7. Use fake adapters and fresh synthetic roots for tests. Do not run live API calls or resolve real credentials without a separate operator GO for the exact account, purpose, operation, bounds, and rollback.

Stop on unknown endpoint permissions, actor, account, target, parent, final content, or unresolved prior effect. Escalate the specific gap rather than weakening a guard.

## Read next

- [Repository rules](../../../AGENTS.md)
- [Context map](../../../docs/CONTEXT_MAP.md)
- [Mutation parity](../../../docs/MUTATION_PARITY.md)
- [Recruitment session contract](../../../docs/RECRUITMENT_SESSION.md)
- [Mutation runtime](../../../src/threads_platform/standalone/mutations.py)
- [Threads API client](../../../src/threads_platform/api/client.py)
- [Mutation runtime tests](../../../tests/test_mutation_runtime.py)
- [Mutation API tests](../../../tests/test_mutation_api.py)