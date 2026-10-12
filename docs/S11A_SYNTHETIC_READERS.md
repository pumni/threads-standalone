# S11A-1 synthetic browser readers

This document describes the isolated S11A-1 reader foundation for issue [#63](https://github.com/pumni/threads-standalone/issues/63), under parent roadmap [#57](https://github.com/pumni/threads-standalone/issues/57). It consumes local, synthetic HTML and does not start Chromium, navigate, access Threads, or connect to the S6 queue. S6 integration and live UAT are not included.

## Reader boundary

`SyntheticHtmlBrowserReader` accepts an immutable `SyntheticHtmlDocument` and a `SyntheticBrowserSessionContext` containing a synthetic account UUID. The document must include one `meta[name="synthetic-account-id"]` marker matching that UUID. This keeps fixtures from crossing account scopes; it is not proof of a real Threads login or actor identity.

The reader recognizes only fixture-defined accessible roles and labels: one `main` landmark, search-result batch regions, post articles, a unique `Post permalink` link, a unique `Author` link, an optional `Post text` paragraph, and an optional `Replying to` link. It never uses DOM order to infer a parent. A parent is returned only from that explicit link. A missing or uncollected parent remains `null` and makes conversation context partial. Parsing is additionally capped at 1,000,000 HTML characters and 5,000 nodes to keep fixture processing bounded.

The synthetic document parser is deliberately separate from `PlaywrightBrowserEngine`. The class has no navigation or browser-start operation. S11A-1 adds no production Threads selectors, search command, live capability flag, or `BROWSER_UI` provenance. An authorized future reader must be a separate implementation and must pass through the same bounded V1 normalization boundary.

## Search output

`SyntheticBrowserSearchAdapter.search(query, max_items=20)` returns `BrowserSearchResultV1`:

```json
{
  "result_version": 1,
  "query": "design role",
  "observations": [
    {
      "thread_ref": "https://www.threads.com/@synthetic/post/fixture_1",
      "author_username": "synthetic",
      "text_excerpt": "A bounded synthetic excerpt",
      "position": 0,
      "source": "SYNTHETIC_FIXTURE",
      "context_status": "UNKNOWN",
      "truncated": false
    }
  ],
  "truncated": false,
  "source": "SYNTHETIC_FIXTURE"
}
```

Search trims an explicit, single-line query and rejects queries over 120 characters. The fixture may contain at most five result batches and 100 inspected cards. The requested output limit is 1–20; excess results are omitted in fixture order and set `truncated`. Excerpts are normalized to 500 characters. The reader also preserves an explicit fixture truncation marker. Duplicate canonical permalinks with identical evidence are deduplicated; conflicting evidence for one permalink fails closed. Search context is always `UNKNOWN` because this reader does not fetch conversation context.

## Conversation output

`SyntheticBrowserConversationAdapter.read(exact_target, max_items=20)` returns `BrowserConversationResultV1`:

```json
{
  "result_version": 1,
  "root_thread_ref": "https://www.threads.com/@synthetic/post/root_1",
  "observations": [
    {
      "thread_ref": "https://www.threads.com/@synthetic/post/root_1",
      "author_username": "synthetic",
      "text_excerpt": "The exact root post",
      "parent_ref": null,
      "position": 0,
      "truncated": false
    },
    {
      "thread_ref": "https://www.threads.com/@reply_author/post/reply_1",
      "author_username": "reply_author",
      "text_excerpt": "A bounded reply excerpt",
      "parent_ref": "https://www.threads.com/@synthetic/post/root_1",
      "position": 1,
      "truncated": false
    }
  ],
  "context_status": "COMPLETE",
  "truncated": false,
  "source": "SYNTHETIC_FIXTURE"
}
```

The requested canonical target must match both the fixture page URL and the conversation landmark's `data-target`. The exact root article must be present and its author marker must agree with the permalink. Context starts as `COMPLETE`, `PARTIAL`, or `UNKNOWN` only when the fixture explicitly says so; absent completeness evidence stays `UNKNOWN`. An omitted parent reference, an uncollected parent target, explicit partial status, or truncated content makes the result partial. An entirely absent reply cannot be inferred unless the fixture marks the context partial. Parent cycles, conflicting duplicate post evidence, off-target roots, and malformed parent references fail closed.

## Provenance and identity

Every result produced by these readers says `SYNTHETIC_FIXTURE`. The item-level permalink and author marker are evidence inside the local fixture only. Canonicalization produces a Threads permalink-shaped string for queue and review identity; it does not prove that the URL exists, identify an API media/reply ID, verify a Threads actor, or establish that context is current or complete. Consumers must retain the source and context status and must not translate a permalink into an API media ID.

## Safe failures

Readers expose only the existing bounded error-code interface. Relevant codes are:

| Code | Meaning |
| --- | --- |
| `BROWSER_CONTRACT_MISMATCH` | Invalid query, target, or requested result bound |
| `BROWSER_REQUIRED_MARKER_NOT_FOUND` | Required accessible landmark or marker is absent |
| `UNSUPPORTED_UI_STATE` | Changed fixture structure, ambiguous marker, duplicate conflict, invalid relationship, missing root, or exceeded bound |
| `REMOTE_STATE_UNCERTAIN` | Fixture URL is off-origin, redirected, or does not match the exact requested target/query |
| `BROWSER_SESSION_MISMATCH` | Fixture account UUID differs from the injected account context |
| `SESSION_EXPIRED` | Synthetic session marker is absent or reports loss |
| `CHALLENGE_REQUIRED` | Fixture exposes a login/challenge alert |
| `BROWSER_READ_TIMEOUT` | The injected reader did not finish within the existing 30-second read bound |
| `BROWSER_PROCESS_CRASHED` | An injected reader reports that its browser process ended |

Error messages contain only these codes. No raw document, cookie, request/response data, or account content is persisted.

## Proposed S6 integration after merge

1. Keep the queue's account UUID, canonical permalink, and API media/reply identifiers as separate fields. Do not derive an API ID from a browser URL.
2. At a later reviewed integration boundary, map `BrowserSearchResultV1.observations` into queue candidates while carrying `result_version`, item and result `source`, `context_status`, `position`, and `truncated` without upgrading fixture provenance.
3. Pass the exact queued permalink into `SyntheticBrowserConversationAdapter` in offline integration tests and preserve the returned root, evidenced `parent_ref` values, and completeness status.
4. Add the synthetic search → queue → ranking integration only after S6 has merged. Keep real browser reads disabled pending the separate #64 authorization decision and bounded UAT; this PR grants no such permission.

S11A-1 is the typed, synthetic-only boundary for that later work. It does not implement queue persistence or CLI wiring.
