# S11A-2 synthetic browser opportunity bridge

This document describes the source-only bridge for issue [#70](https://github.com/pumni/threads-standalone/issues/70), under [#63](https://github.com/pumni/threads-standalone/issues/63) and roadmap [#57](https://github.com/pumni/threads-standalone/issues/57). It consumes the accepted S11A-1 V1 search and conversation results. It does not start a browser, access a network, write the S6 queue, add CLI wiring, or authorize live collection.

## Typed boundary

`build_browser_opportunity_proposals` is a synchronous pure function:

```python
build_browser_opportunity_proposals(
    search: AccountScopedSearchResult,
    *,
    expected_account_uuid: UUID,
    conversations: Sequence[AccountScopedConversationResult] = (),
) -> BrowserOpportunityProposalBatch
```

The caller pairs each V1 result with the UUID from its local account/session context. The bridge requires each scope to match `expected_account_uuid`; this is an account isolation binding, not authentication of a Threads actor. Each conversation envelope also carries the exact requested target. The bridge canonicalizes it and requires that it match both a search result and `BrowserConversationResultV1.root_thread_ref`.

The batch preserves V1 version, query, overall truncation and `SYNTHETIC_FIXTURE` source. Each bounded proposal preserves:

| Proposal field | Meaning |
| --- | --- |
| `account_uuid` | The exact account scope supplied to the bridge. |
| `target_permalink` | Canonical `https://www.threads.com/@user/post/id` identity; not an API media ID. |
| `source`, `context_provenance` | Both remain `SYNTHETIC_FIXTURE`. Neither is rewritten to `MANUAL_URL` or `BROWSER_UI`. |
| `author_marker`, `author_identity_status` | The fixture's permalink author marker, explicitly `UNVERIFIED`; this is not actor authentication. |
| `excerpt`, `position` | Search excerpt (at most 500 characters) and original bounded search position. |
| `search_context_status`, `context_status` | Original search status and effective conversation completeness. Search results are required to remain `UNKNOWN`. |
| `truncated`, `search_observation_truncated`, `search_result_truncated`, `conversation_result_truncated` | Combined warning and each available source-level signal; conversation truncation is `null` when no conversation was supplied. |
| `conversation_observations` | Bounded root/reply excerpt, position, truncation, unverified author marker and explicit parent permalink. A missing parent stays `null`; DOM order is never used to fill it. |
| `review_state`, `reason_codes` | Always `NEEDS_CONTEXT`, with fixed reasons for unknown/partial context, truncation, unverified identity and the queue-v1 source limit. |

Canonical duplicates are collapsed deterministically to the lowest input position. Compatible truncated excerpts may be combined by keeping the longer bounded excerpt and retaining the truncation signal. Conflicting text, author/URL, parent, account or target evidence fails closed with a fixed bridge error code. An asserted `COMPLETE` conversation is downgraded to `PARTIAL` when content is truncated or a reply parent is absent/unobserved. `UNKNOWN` is never upgraded to `COMPLETE` from item order.

Bridge failures expose only these codes:

| Code | Meaning |
| --- | --- |
| `BRIDGE_ACCOUNT_MISMATCH` | Search/conversation scope differs from the expected account UUID. |
| `BRIDGE_INVALID_PROVENANCE` | Result or item source is not `SYNTHETIC_FIXTURE`. |
| `BRIDGE_INVALID_RESULT` | V1 shape, bound, query or observation value is invalid. |
| `BRIDGE_INVALID_TARGET` | Target is not a canonicalizable Threads permalink. |
| `BRIDGE_RESULT_LIMIT_EXCEEDED` | Search, conversation or proposal input exceeds 20 items. |
| `BRIDGE_TARGET_MISMATCH` | Requested conversation target, result root or search target differs. |
| `BRIDGE_CONTRADICTORY_OBSERVATIONS` | Duplicate, author, excerpt, root or parent evidence conflicts. |

## S6 seam and current queue limit

S6's persisted `opportunity_queue_v1` is account-isolated, canonical-target deduplicated and limited to 20 active items. Its strict source field accepts only `MANUAL_URL`; its context provenance accepts `NONE` or `OPERATOR_SUPPLIED`. The store derives `READY` only from complete operator-supplied context. The bridge intentionally does not import or invoke `OpportunityQueueStore`, `import_candidates`, or any v1 persistence path. Its synthetic proposal remains `NEEDS_CONTEXT` even when a fixture labels its context complete.

The test sink models only the in-memory intake properties needed here: exact account partitioning, canonical deduplication, atomic capacity checking and at most 20 active proposals in each account. It does not mimic persistence, locks, operator outcomes or a queue command.

## Proposed persistent v2 schema (follow-up only)

Persistence is out of scope for S11A-2. A later separately approved queue-v2 design could use the following exact keys and constraints. The source union is only `MANUAL_URL | SYNTHETIC_FIXTURE`; a future `BROWSER_UI` source would require its own separately reviewed authorization and versioned contract. This is one valid synthetic entry example.

```json
{
  "version": 2,
  "account_uuid": "00000000-0000-4000-8000-000000000001",
  "opportunities": [
    {
      "id": "00000000-0000-4000-8000-000000000002",
      "target_url": "https://www.threads.com/@user/post/id",
      "source": "SYNTHETIC_FIXTURE",
      "author_marker": "user",
      "author_identity_status": "UNVERIFIED",
      "context": null,
      "context_provenance": "SYNTHETIC_FIXTURE",
      "context_completeness": "UNKNOWN",
      "source_evidence": {
        "excerpt": "bounded search excerpt or null",
        "position": 0,
        "search_context_status": "UNKNOWN",
        "context_status": "UNKNOWN",
        "truncated": false,
        "search_observation_truncated": false,
        "search_result_truncated": false,
        "conversation_result_truncated": null,
        "conversation_observations": [
          {
            "target_url": "https://www.threads.com/@user/post/id",
            "author_marker": "user",
            "author_identity_status": "UNVERIFIED",
            "excerpt": "bounded excerpt or null",
            "position": 0,
            "parent_url": null,
            "is_root": true,
            "truncated": false
          }
        ]
      },
      "created_at": "2026-10-12T00:00:00Z",
      "updated_at": "2026-10-12T00:00:00Z",
      "state": "NEEDS_CONTEXT",
      "skip_reason": null
    }
  ]
}
```

For `SYNTHETIC_FIXTURE`, `source_evidence` is required, author identity is `UNVERIFIED`, and `context` is null until an operator supplies separate context. A `MANUAL_URL` entry instead has null `source_evidence` and `author_marker`, `author_identity_status=UNKNOWN`; its context provenance remains `NONE | OPERATOR_SUPPLIED` with existing v1 context semantics. If an operator later adds context to a synthetic record, `source` and `source_evidence` remain unchanged while `context_provenance` becomes `OPERATOR_SUPPLIED` and the operator's context status is stored separately from immutable `source_evidence.context_status`.

The v2 validator would enforce the same 20 active / 40 retained queue bounds, account UUID and target uniqueness, excerpt and observation bounds, exact root identity, source/evidence consistency, valid parent URLs, and fail-closed conflict checks. `READY` would require `context_provenance=OPERATOR_SUPPLIED` and `context_completeness=COMPLETE`; fixture completeness alone would remain `NEEDS_CONTEXT`. No API media ID would be stored or derived.

An explicit migration would be a separately scoped, operator-invoked step: (1) leave `opportunity_queue_v1/accounts/<account_uuid>.json` byte-for-byte unchanged as the rollback source; (2) validate it with the existing strict reader; (3) map every v1 item into `opportunity_queue_v2/accounts/<account_uuid>.json` as `source=MANUAL_URL`, with unchanged target/context/timestamps/state, unchanged `NONE | OPERATOR_SUPPLIED` context provenance/completeness, `source_evidence=null`, `author_marker=null`, and `author_identity_status=UNKNOWN` because v1 did not store author identity; (4) write a sibling staging file under the v2 account directory, strictly reread it, compare counts and invariants, then atomically install the v2 file; (5) retain v1 read compatibility or a documented rollback path until explicit acceptance. Synthetic records would enter only through a new v2-aware, separately reviewed intake seam. This proposal does not implement that migration or alter v1 reads/writes.

## Tests and remaining integration steps

The synthetic E2E tests run local HTML through `SyntheticHtmlBrowserReader`, the S2 V1 adapters, the bridge and a bounded in-memory sink. They cover zero/one/20/21 observations, canonical duplicates and conflicts, account isolation, partial and truncated conversation evidence, explicit nested parents, missing parents, invalid provenance, exact-target mismatch, and absence of network/browser/persistence effects.

After S6 integration planning is accepted, the next safe steps are: review/accept the v2 proposal; implement its strict codec and explicit migration in an owned queue scope; add an explicit queue-v2 synthetic intake that keeps `SYNTHETIC_FIXTURE` and `NEEDS_CONTEXT`; keep the current manual CLI/v1 path compatible; and run source plus installed-wheel regression. Any production/live reader remains disabled and separately gated by issue #64 authorization and UAT.
