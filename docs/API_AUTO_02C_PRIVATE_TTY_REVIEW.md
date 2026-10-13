# API-AUTO-02C: Private TTY review

This micro-slice presents one already validated `PrivateReviewPreview` on an
interactive terminal and returns an in-memory `PrivateTtyReviewReceipt` only
after an exact operator response. It is synthetic testable code; it does not
authenticate a live Threads observation.

## Review boundary

`run_private_review_tty(...)` accepts the preview, its content packet, current
planner DTOs, display snapshot, three terminal streams, an environment mapping,
and an injected UTC clock. It re-runs
`build_private_review_preview(...)` before displaying anything and requires
the resulting complete preview to equal the supplied value. It repeats that
validation after the operator responds. The second check validates the same
supplied in-memory evidence; a future dispatch path must independently reacquire
and verify current official API context.

Before rendering, the function requires `stdin`, `stdout`, and `stderr` all to
report `isatty() is True`, and refuses nonempty `CI`, `GITHUB_ACTIONS`,
`TF_BUILD`, `BUILDKITE`, `TEAMCITY_VERSION`, or `JENKINS_URL` values. It writes
the full preview and challenge only to `stderr`; it never writes to `stdout`.
Every variable string is JSON quoted so content cannot imitate a review label.
Unsafe UTF-8, terminal controls, bidi/format characters, surrogates, and
Unicode line/paragraph separators stop before output. Nothing is silently
rewritten or truncated. The complete output is limited to 64 KiB and 220 lines.

The display includes the account UUID and actor, action, source and
provenance, exact target IDs, complete root/parent/reply text for replies, the
final normalized operator text, content origin, rights basis, use declaration,
source attribution, job-facts declaration and digest, packet and preview SHA-256
values, and expiry times. Post actions identify the selected account and show
that no remote target IDs apply.

The single bounded input read is `readline(128)`. The only affirmative line is
exactly:

```text
CONFIRM <first 16 lowercase hex characters of preview_sha256> <first 16 lowercase hex characters of packet_revision_sha256>
```

It must end with one normal line ending. Empty input, EOF, `APPLY`, a wrong or
uppercase digest, extra characters, overlong input, or interruption returns no
receipt. This challenge prevents accidental confirmation; it is not a secret,
cryptographic authorization token, or dispatch permission.

## Result limits

`CONFIRMED_FOR_REVIEW_ONLY` returns a frozen receipt containing the full packet
revision digest, preview digest, account UUID, action, and confirmation time.
The receipt is an ephemeral record that a person reviewed this exact preview.
It is not a durable approval, `ApprovalInput`, `ApprovedPacketSummary`,
officially authenticated observation, single-use grant, or dispatch authority.
It has no approval ID or `dispatchable` field, is not stored, and must never be
accepted by a later approval store as proof of authorization. This module does
not read or write files, resolve credentials, call an API, launch a browser,
invoke an AI provider, create a proposal, or dispatch an action.

All examples and tests use synthetic account, actor, target, content, and rights
data. The slice does not register a CLI command; private TTY integration and a
durable approval store remain separately reviewed gates.
