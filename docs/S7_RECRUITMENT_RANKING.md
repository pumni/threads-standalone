# S7 deterministic recruitment qualification

S7 adds a local, deterministic ranking view over the account-scoped S6 opportunity queue. It uses only the operator-supplied queue context and the existing built-in recruitment exclusions. It does not search Threads or infer content from a permalink.

## Command

Run the command from a private interactive terminal with the account's existing local data root configured:

```powershell
threads-local recruitment opportunity-rank synthetic_account --limit 10
```

`--limit` accepts 1 through 20 and defaults to 10. The command reads the queue for the selected account, evaluates its bounded entries, and writes the ranked targets and fixed reason codes to private terminal `stderr`. It does not print operator context, draft text, or raw queue JSON. Redirected, piped, CI, and non-TTY use fail closed.

The output keeps the account UUID, opportunity ID, canonical permalink, `MANUAL_URL` source, and context-completeness assertion together. The ranking is ephemeral: it does not change queue entries, timestamps, S4 outcome records, or any v1 Nurture/journal schema.

## Classification and state

The classifier returns one of:

| Classification | Meaning |
| --- | --- |
| `JOB_SEEKER` | Context has an explicit job-seeking signal. Student status, a job family, or a location cue alone cannot establish this class. |
| `RECRUITER` | Context has an explicit hiring or candidate-search signal. These items are shown as suppressed because S7's shortlist targets job seekers. |
| `GENERAL_DISCUSSION` | Context refers to employment but does not establish a current job-seeking intent. |
| `UNKNOWN` | Context has no clear recruitment intent or contains unresolved contradictory/mixed intent. |

The qualification state is independent of the S6 queue state:

| State | Meaning |
| --- | --- |
| `ELIGIBLE_FOR_REVIEW` | Explicit job-seeking language, a `COMPLETE` operator context assertion, and no detected suppression signal. This only qualifies the item for a human S3 review; it is not approval or permission to reply. |
| `NEEDS_CONTEXT` | Context is missing, partial, unknown, ambiguous, mixed, contradictory, or a possible text duplicate. |
| `SKIPPED` | S6 already marked the item skipped, or a deterministic exclusion, duplicate, no-contact, prior-contact, sensitive-data, recruiter/general-discussion, or blocking S4 outcome applies. |

S4 `HANDOFF_UNRESOLVED`, `AMBIGUOUS`, and `OPERATOR_REPORTED_SENT_UNVERIFIED` outcomes suppress a target. `ABORTED_NOT_SENT` does not suppress it. Reading S4 status is local and read-only.

## Scores and signals

Scores use fixed points: `JOB_SEEKER_BASE` 60, `RECRUITER_BASE` 35, `GENERAL_DISCUSSION_BASE` 15, and `UNKNOWN_BASE` 0. One explicit job-family group adds 8; a visible question mark adds 3. Suppression components subtract 100, and scores are clamped to 0–100. Context completeness, location, and recency do not add points. Results sort by state (`ELIGIBLE_FOR_REVIEW`, `NEEDS_CONTEXT`, `SKIPPED`), score descending within each state, then original queue position.

Job family is reported only when a fixed term group is explicit (`IT`, `SALES`, `MARKETING`, `FINANCE`, `HUMAN_RESOURCES`, `CUSTOMER_SUPPORT`, or `ENGINEERING`); multiple groups return `MULTIPLE`. Student, part-time, and internship terms are evidence signals only. They do not create job-seeking intent.

Location output reports explicit cues such as remote, hybrid, onsite, or a named place. There is no account-level location preference in the S6 contract, so `location_relevance` remains `UNASSESSED_NO_ACCOUNT_PREFERENCE`; S7 does not claim a geographic fit. Recency is only reported as `OPERATOR_ASSERTED_UNVERIFIED` when the context contains a recognized recency phrase. S6 capture timestamps are not post timestamps and are never used as recency evidence.

The score and reason codes never establish author identity. `COMPLETE` is an operator assertion about the supplied context, not verified coverage of the Threads conversation. S6 has no trusted conversation-coverage field, so unanswered-question status is always `UNASSESSED_COVERAGE_UNVERIFIED`; a question mark is only reported as `QUESTION_PRESENT`.

S6 already deduplicates canonical targets. S7 also suppresses exact normalized duplicate context and marks a near-duplicate context as `NEEDS_CONTEXT` using a fixed token-overlap heuristic. These are review signals, not identity proof. Recruitment exclusions reuse the built-in Nurture v1 preset's text exclusion terms without changing its schema, fingerprint, or API discovery path.

## Safety and limits

- Reads at most the S6 retained queue bound (40 entries), which allows up to 20 active entries plus skipped history; output is capped at 20.
- Uses local queue and outcome reads only. It does not start Playwright, call Threads APIs, access the network, call an AI provider, or publish/reply/follow.
- Raw context is not rendered. Target URLs and fixed reason codes are shown only after the private TTY guard passes.
- A positive ranking is a shortlist aid. Exact target, context, draft, account, and human approval remain in the existing S3/S4 workflows.
- Tests use synthetic URLs, IDs, context, accounts, and temporary data roots. No live Threads UAT was performed.
