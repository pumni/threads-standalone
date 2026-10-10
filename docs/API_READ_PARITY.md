# SPLIT-02 API read parity

Frozen behavior source: `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`. This table records implementation and synthetic-test coverage in the destination. It does not claim live verification against Threads.

| CLI command | Runtime/client method | Contract coverage |
| --- | --- | --- |
| `threads-local api quota ALIAS` | `quota` / `get_publishing_quota` | Request fields and nested publishing/reply quota response; normalized HTTP errors. |
| `threads-local api media ALIAS MEDIA_ID` | `media` / `get_media` | Requested fields, media response mapping, HTTPS URL validation. |
| `threads-local api post-insights ALIAS MEDIA_ID` | `post_insights` / `get_post_insights` | Metric names/order, lifetime period, malformed metric envelopes. |
| `threads-local api public-profile ALIAS USERNAME` | `public_profile` / `get_public_profile` | Profile lookup mapping and bounded fields. |
| `threads-local api profile-posts ALIAS USERNAME [--after CURSOR] [--limit N]` | `profile_posts` / `get_profile_posts` | Username, limit, fields, cursor continuation and response mapping. |
| `threads-local api search ALIAS QUERY --mode {keyword,tag} --type {top,recent} [--after CURSOR] [--limit N]` | `search` / `search_threads` | Query, mode/type, optional date range at client boundary, limit, cursor, and response mapping. |
| `threads-local api mentions ALIAS [--after CURSOR] [--limit N]` | `mentions` / `get_mentions` | `me/mentions` fields, continuation cursor, response mapping. |
| `threads-local api replies ALIAS THREAD_ID [--after CURSOR]` | `replies` / `get_replies` | Read-only replies edge, exact fields, reverse flag, cursor and parent IDs. |
| `threads-local api conversation ALIAS THREAD_ID [--after CURSOR]` | `conversation` / `get_conversation` | Read-only conversation edge and cursor/response contract. |

The destination also tests local account add/list/credential-reference setup, UUID/schema preservation, per-invocation environment credential resolution, invalid cursor rejection before account lookup, and sanitized API/credential failures. HTTP tests use `httpx2.MockTransport`; no test calls Threads.

## Error and output contract

HTTP failures are normalized to bounded error codes; response bodies and transport details are not attached to printed errors. Authentication, permission, not-found, invalid-request, rate-limit, server, contract-mismatch, and timeout/transport paths have synthetic coverage. CLI strings, text fields, URLs, and cursors are bounded/sanitized before output. Invalid account/API arguments fail before request dispatch where the frozen runtime requires local validation.

## Deferred evidence

`IMPLEMENTED` and `SYNTHETICALLY TESTED` describe destination behavior exercised by the listed fake-response tests. There is no `LIVE-VERIFIED` claim for this destination. Browser behavior, API mutations, local operation journals, state migration, Controller/Worker, and database behavior are not part of this checkpoint.
