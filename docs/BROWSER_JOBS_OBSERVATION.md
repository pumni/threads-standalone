# BJ-02 synthetic job observation

BJ-02 adds an in-memory observation contract for synthetic feed/search cards. It is not a browser reader: the module imports no browser runtime, has no network or filesystem implementation, and cannot create `BROWSER_UI` provenance. Live collection remains gated by #64 and #103; production reader work remains separately gated by #106.

## Scope and entry point

`observe_fake_job_feed(...)` accepts a `FakeJobBrowserSession` protocol, one account UUID, one bounded query, explicitly selected exact targets, an injected monotonic fake clock, a stop signal, a caller-supplied `capture_id`, and a caller-supplied `observed_at_utc`. The session must return `FakeJobFeedBatchV1` and `FakeJobPostDetailV1` objects created from synthetic in-memory data.

The adapter reads the first visible tranche, validates and copies each card's synthetic URL and local card ID, then scrolls by one tranche. It repeats this in first-seen order and deduplicates by `(account_uuid, exact canonical URL)`. Thus a later fake DOM may evict old cards without losing their previously captured targets. Re-observing an existing target does not reorder it. Reusing one card ID for a different target, changing the actor label for the same target, or returning another account stops the run.

Detail reads happen only after the feed pass and only for URLs explicitly listed in `selected_targets`. Each selection must exactly match a captured URL. The returned page URL, canonical URL, account, and any visible feed/detail author marker are checked against the requested target. The fake protocol is not used to normalize URLs, guess IDs, or follow a quote/reply target.

## Provenance and observation fields

Every `BrowserJobObservationV1` emitted by the adapter has fixed provenance:

| Field | Value |
| --- | --- |
| `source` | `SYNTHETIC_FIXTURE` |
| `text_provenance` | `SYNTHETIC_FIXTURE_TEXT` |
| `completeness_basis` | `FIXTURE_DECLARED` |
| `author_marker` | `UNVERIFIED` |

## Capture identity, time, and revisions

Each call requires a synthetic `capture_id` of 1–128 ASCII letters, digits, or `._:-` characters. The caller must supply a distinct ID for each capture; the adapter does not generate UUIDs or use a clock. It also requires an aware UTC `observed_at_utc` value. Naive or non-zero-offset timestamps and invalid capture IDs fail before the fake session is read, using `INVALID_CAPTURE_TIMESTAMP` or `INVALID_CAPTURE_ID`. The accepted timestamp is normalized to `datetime.UTC` and stored as the observation event time.

`observation_id` is derived from account, exact post URL, the injected capture ID and UTC event time, and the selected detail order. Separate captures therefore have distinct observation IDs even when they inspect the same post with unchanged text. `post_revision_id` remains based on the exact supplied body and post identity: unchanged text may share a revision across captures, while changed text produces a different revision. `observed_at_utc` is separate from the optional source claim `published_at_utc`; the adapter never substitutes one for the other.

The fake actor label is used only to detect a mismatch between feed and detail fixtures. It never authenticates an account. Quote/repost authors and targets remain separate fields from the top-level author and body; reply parent information is also separate. The adapter does not merge quoted content, comments, replies, or parent text into the selected post body.

`FULL_VISIBLE` means the whole body represented by the synthetic detail fixture was returned without clipping or unresolved collapsed content. It says nothing about real Threads content. A 10,000-code-point cap, a fixture-declared partial/collapsed body, or uncertain visibility cannot become `FULL_VISIBLE`. Missing text or image-only content is `UNKNOWN`. A contradictory detail marked `image_only` while also supplying nonempty text or declaring `FULL_VISIBLE` fails the run with `FAILED` / `INVALID_SESSION_STATE` and emits no observation. Missing actor attribution or relationship evidence can make the observation status `PARTIAL` even if the visible text itself is complete. A source-published timestamp is retained only when timezone-aware and normalized to UTC; it remains separate from the caller-supplied capture time.

`to_parser_observation(...)` maps a DTO to BJ-01's pure `PostObservation`, preserving the exact text and synthetic provenance. It cannot map a manually supplied URL or browser UI source. Tests pass that value to `extract_vietnamese_vacancies`; Unicode evidence spans therefore remain offsets into the exact fixture body.

## Run status and fixed reasons

The run status is independent of each post's completeness:

| Run status | Meaning |
| --- | --- |
| `COMPLETE` | The bounded fake operation finished. Individual records may still be `PARTIAL` or `UNKNOWN`. |
| `CAP_REACHED` | A configured hard bound stopped further fake work. Already complete individual posts remain complete. |
| `STOPPED` | A stop signal, deadline, changed target/actor/account, redirect, challenge, login wall, rate limit, or missing selected post stopped the run. |
| `FAILED` | Input or fake session state was invalid or unclassified. No fallback or retry occurs. |

Reasons are fixed enum codes, not exception text or source content. DTO `repr` methods redact URLs, account IDs, card IDs, actor labels, and post text. The run reports only bounded counts and reason codes.

## Bounds

Defaults are the BJ-00/BJ-02 maxima. `ObservationLimits` may lower them; construction rejects any value above the hard maximum.

| Limit | Maximum |
| --- | ---: |
| Query | 120 Unicode code points |
| Visible feed batches / scroll traversals | 5 |
| Cards per tranche | 20 |
| Examined candidate appearances | 100 |
| Selected exact detail reads | 20 |
| Captured body per post | 10,000 Unicode code points |
| Fake operation duration | 30 seconds |

Candidate appearances include re-observations; captured target count is separately deduplicated. A card or candidate cap returns `CAP_REACHED` with a fixed reason. A detail body clipped at the text cap remains available only as a 10,000-code-point `PARTIAL` observation with `text_truncated=true`. Query, target, account, and unclassified-state violations fail closed before any fallback. The injected clock and stop signal make deadline and cancellation behavior deterministic.

## Synthetic-only example

```python
account_uuid = UUID("00000000-0000-4000-8000-000000000101")
target = "https://threads.example.invalid/@synthetic/post-001"
card = FakeJobCardV1(
    account_uuid=account_uuid,
    local_card_id="card-synthetic-001",
    canonical_post_url=target,
    author_marker="synthetic-employer-001",
)
batch = FakeJobFeedBatchV1(
    batch_index=0,
    account_uuid=account_uuid,
    query="tuyển dụng synthetic",
    page_url=SYNTHETIC_SEARCH_URL,
    state=FakePageState.READY,
    cards=(card,),
    has_more=False,
)
```

An implementation of the protocol for tests stores such batches/details in memory, replaces its visible cards on scroll, and records method calls. The example does not create a browser session or open the URL.

## Excluded capabilities

This slice does not add Playwright/Selenium use, HTML or fixture-file loading, HTTP/socket/subprocess/CDP access, real Threads URLs or selectors, account profiles, cookies, screenshots, persistent state, CLI/watch loops, contact extraction/storage, UI effects, or a live-source adapter. Synthetic success is not evidence of platform permission, live visibility, or complete real-world coverage.
