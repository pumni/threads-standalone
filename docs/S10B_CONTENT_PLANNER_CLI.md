# S10B offline content planner CLI

S10B registers a private, local command surface over the accepted S10A
`RecruitmentContentPlannerStore`. It creates and reviews original operator
content and reads the local calendar. It does not invoke the Threads API,
Playwright, a browser, a scheduler, an AI provider, media transfer, or a remote
publishing operation.

## Commands

```text
threads-local recruitment content-add ALIAS --text-file PATH --scheduled-utc 2026-10-13T09:00:00Z --assert-rights --assert-consent
threads-local recruitment content-list ALIAS
threads-local recruitment content-show ALIAS --id PACKET_UUID
threads-local recruitment content-due ALIAS
threads-local recruitment content-upcoming ALIAS
threads-local recruitment content-approve ALIAS --id PACKET_UUID
threads-local recruitment content-reject ALIAS --id PACKET_UUID
```

`content-add` reads a bounded UTF-8 file, checks that it is a regular file
without a symlink or reparse point in its path, rejects content paths inside
the repository or local planner data root, and requires both explicit rights
and consent assertion flags. S10A validates the 500-character text bound,
operator-authored provenance, private-content patterns, UTC schedule window,
account limits and duplicate behavior. These are unverified operator
assertions. Neither software nor those assertions establish copyright,
consent, accuracy, identity, or platform permission. The CLI does not accept
post text as a command-line argument and does not echo it after creation.

Generic list, due and upcoming output includes packet UUID, UTC schedule,
editorial state, provenance, rights assertion state, platform permission
state, and `NOT_PUBLISHED_BY_PLANNER`. It does not include post text, notes,
attachment URLs or review notes. Due/upcoming use the S10A local clocked
calendar behavior and exclude rejected packets.

## Private review and stale-data protection

`content-show`, `content-approve` and `content-reject` require stdin, stdout
and stderr to be TTYs and reject recognized CI environments before reading
packet details. Full packet JSON and its S10A versioned fingerprint are written
to stderr only. The displayed packet contains all S10A packet fields, including
the exact operator text, rights and consent assertions, optional inert
metadata, private notes, schedule, review fields and fixed remote-publication
status.

Approve/reject print an exact confirmation containing the action, account
UUID, packet UUID and the fingerprint of the displayed immutable snapshot.
The operator must type that exact line after seeing the snapshot. Rejection
also asks for a bounded local reason on the private terminal. No account lock
is held while waiting for either input. Only after confirmation does the CLI
call `review_if_unchanged()` exactly once with the snapshot fingerprint and
expected `DRAFT` status. A concurrent edit or decision returns S10A's fixed
`CONTENT_PLANNER_REVIEW_STALE` error; the CLI does not retry or apply a stale
decision. It does not call the legacy `approve()` or `reject()` methods.

`READY_FOR_MANUAL_PUBLICATION` is an editorial state only. It never means that
content was sent, accepted by Threads, or published. Every success response
continues to show `NOT_PUBLISHED_BY_PLANNER`.

## Integration and limits

The CLI maps a local account alias to its UUID with `LocalAccountStore`, then
calls only the S10A local store. S10A owns account locking, strict JSON
validation, duplicate detection and atomic persistence. The CLI does not alter
the v1 planner schema or S4 journal. Fixed CLI errors do not include paths,
text, notes, URLs or credentials.

The text scanner is a conservative pattern check, not a full personal-data
classifier. Do not enter private applicant CVs, personal data, customer content
or credentials. The CLI does not verify operator rights assertions, establish
platform permission, or publish content. Live Threads operations, API
publishing, browser sending, automated scheduling, AI generation and media
transfer remain outside this scope and require separate authorization.
