# Browser Jobs foreground CLI (BJ-04B)

This slice is a **`READY_SYNTHETIC_DOM_E2E` implementation candidate** after the
offline Chromium path and recruiter-only archive have passed the synthetic
acceptance checks. It is not a live Threads reader, customer delivery, or
production automation claim.

The installed command is:

```text
threads-local recruitment browser-watch
```

It supports five explicit source values:

- `fixture` validates a bounded UTF-8 fixture, maps it to BJ-02 fake session
  objects in memory, then calls the unchanged BJ-04A engine.
- `dom-fixture` renders bounded local synthetic search/feed/detail HTML in an
  ephemeral headless Chromium context, snapshots exact root-card evidence, maps
  it to the same BJ-02 fake session contract, then calls the unchanged BJ-04A
  engine. It loads documents with `set_content`, disables JavaScript, blocks
  every browser request, and never navigates to the reserved post URLs.
- `ui-fixture` launches ephemeral Chromium and exercises the B1 semantic UI
  reader against a private local HTML manifest. It searches by the manifest's
  query, reads visible feed cards before each bounded scroll, opens exact
  reserved post URLs through an allowlisted local route, and maps verified
  root-card snapshots to BJ-02's existing synthetic contract. It blocks every
  request outside the exact fixture routes and keeps provenance
  `SYNTHETIC_FIXTURE`.
- `browser` is accepted so the production source has an explicit fixed
  `DISABLED_PERMISSION_PENDING / SOURCE_DISABLED` result. The CLI returns this
  before reading any account, path, fixture, browser profile, or operator root.
- `manual` returns `UNKNOWN / NO_TEXT`. This initial CLI does not accept or
  open a target URL/file, parse a post, or write an archive for manual input.

Actual browser reads remain disabled pending separately verified platform
permissions and operator UAT. Both synthetic sources are offline test
infrastructure only: they have no persistent browser profile, cookies,
account-store, API, CRM, post, reply, like, follow, DM, or background-service
path. Synthetic HTML selectors do not claim compatibility with the live Threads
UI.

## Fixture schema

The fixture file must be in a private directory outside the checkout. It is
limited to 1,100,000 bytes, strict UTF-8 without a BOM, JSON with no duplicate
keys, one to five cycles, and at most 20 posts in each cycle. The accepted
schema is exact; unknown keys fail closed:

```json
{
  "schema_version": 1,
  "fixture_kind": "SYNTHETIC_ONLY",
  "cycles": [
    {
      "targets": [
        "https://threads.example.invalid/@synthetic-shop/post-001"
      ],
      "posts": [
        {
          "url": "https://threads.example.invalid/@synthetic-shop/post-001",
          "body": "Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng.",
          "body_visibility": "FULL_VISIBLE",
          "published_at_utc": "2026-10-12T09:00:00Z"
        }
      ]
    }
  ]
}
```

Every target must use the reserved `https://threads.example.invalid` host and
the ordered target list must match the ordered post list exactly. Each post is
mapped to a synthetic author marker and the supplied synthetic account. The
fixture must contain newly written synthetic content only; phone numbers,
emails, and remote URLs in body text are rejected. A `published_at_utc` value
must be UTC with an explicit offset; `null` means unavailable. `body_visibility`
is `FULL_VISIBLE`, `PARTIAL`, or `UNKNOWN`; a missing body requires `UNKNOWN`.

## Synthetic DOM fixture

The DOM manifest has an exact schema with `fixture_kind: SYNTHETIC_DOM_ONLY`, a
synthetic `query`, one to five cycles, and for each cycle one search/feed HTML
document plus ordered exact-post detail HTML documents. The rendered search
document must contain a visible `main[data-search-query]`, and at least two
vertically distinct `section[data-feed-tranche="0"]` through
`section[data-feed-tranche="4"]` elements. The reader scrolls each later tranche
into the viewport. Each visible feed card must keep its reserved permalink and
synthetic author together inside `article[data-post-card]`. Detail pages must
place the same reserved permalink, synthetic author, and own body inside one
`article[data-root-post]` under `main[data-post-detail]`.

The exact detail body uses `data-own-post-body` and declares
`data-body-visibility="FULL_VISIBLE"`, `"PARTIAL"`, or `"UNKNOWN"`. It can also
declare `data-post-kind="ORIGINAL"`, `"QUOTE"`, `"REPLY"`, or `"REPOST"` on the
root article. Unknown kinds, hidden/missing bodies, collapsed text, and
quote/reply/repost text remain conservative; quoted and sidebar content is not
used as the root post body. The permalink handle must match the root author.
Every feed target must have one matching detail page, in first-seen order.
Repeated cards across tranches are counted as candidates but only the first
matching target is opened once.

The manifest is limited to 1,100,000 bytes. Each tranche is limited to 20 cards;
the full capture is limited to five tranches, 100 candidates, 20 detail reads,
10,000 visible body codepoints per post, and 30 seconds. A canonical digest of
the complete DOM manifest is bound into BJ-04A's existing checkpoint query
identity, so changing HTML with the same run ID returns `CHECKPOINT_CONFLICT`.
The archive and observation provenance remain `SYNTHETIC_FIXTURE`.

Create a private directory and empty private data root outside the checkout as
described below, then write a manifest with this shape (the same card appears
in both scrolled tranches):

```json
{
  "schema_version": 1,
  "fixture_kind": "SYNTHETIC_DOM_ONLY",
  "query": "synthetic recruitment roles",
  "cycles": [
    {
      "feed_html": "<main data-search-query=\"synthetic recruitment roles\"><section data-feed-tranche=\"0\" style=\"height:900px\"><article data-post-card><a data-post-permalink href=\"https://threads.example.invalid/@synthetic-shop/post-001\">open</a><header data-post-header><span data-post-author>@synthetic-shop</span></header></article></section><section data-feed-tranche=\"1\" style=\"height:900px\"><article data-post-card><a data-post-permalink href=\"https://threads.example.invalid/@synthetic-shop/post-001\">open</a><header data-post-header><span data-post-author>@synthetic-shop</span></header></article></section></main>",
      "detail_pages": [
        {
          "target": "https://threads.example.invalid/@synthetic-shop/post-001",
          "html": "<main data-post-detail><article data-root-post data-post-kind=\"ORIGINAL\"><a data-post-permalink href=\"https://threads.example.invalid/@synthetic-shop/post-001\">root</a><header data-post-header><span data-post-author>@synthetic-shop</span></header><div data-own-post-body data-body-visibility=\"FULL_VISIBLE\">Cần tuyển 02 nhân viên bán hàng tại TP.HCM; lương 8–12 triệu/tháng.</div></article></main>"
        }
      ]
    }
  ]
}
```

Save that JSON as `$domFixture` in the private directory and run the installed
CLI:

```powershell
threads-local recruitment browser-watch --source dom-fixture --account test-dom `
  --fixture-file $domFixture --data-root $dataRoot --run-id demo-dom-001
```

The CLI prints only fixed status/reason codes and counts, including the number
of feed batches, examined candidates, unique targets, duplicates, exact details,
and blocked browser requests. It does not print the query, author, permalink,
body, manifest path, data-root path, or alias. Identical replay with the same
run ID leaves archive/checkpoint bytes unchanged.

`--account` accepts only aliases beginning with `test-` or `synthetic-`. A
stable UUID is derived from that alias; the CLI never reads or creates a real
`LocalAccountStore` entry. Use a new alias per synthetic account. The required
`--data-root` is a caller-created, empty, private synthetic directory outside
the checkout. The CLI never creates or overwrites the root. On first use it
adds a private marker only to that empty directory; later resumes may reuse the
marked root. An unmarked nonempty root or unexpected top-level file is rejected.
Its only top-level store directories are `browser_job_watch_v1` and
`job_vacancies_v1`.

## B1 semantic Chromium fixture

`ui-fixture` runs `threads_job_ui_reader.py` itself. It does not call the
`dom-fixture` reader or reuse its `data-*` markers. The bounded manifest has
`fixture_kind: SYNTHETIC_BROWSER_UI`, one query, exactly one watch cycle, one
feed HTML document, and one exact detail HTML document per declared target.
The query is limited to 120 codepoints; the whole JSON file is limited to
1,100,000 bytes. Existing BJ-02 limits remain in force: at most five viewport
batches, 20 visible cards per batch, 100 candidate appearances, 20 unique
exact details, 10,000 body codepoints, and 30 seconds. Navigation and actions
have a two-second timeout; collapsed text is never expanded.

The synthetic page shell needs one visible `header[role="banner"]` profile
link matching the suffix of the synthetic `--account` alias, one `main`, and
for search one `form[role="search"] input[type="search"]` whose value matches
the manifest query. A candidate is a visible top-level `article` or
`[role="article"]`; its own header must bind one permalink and one author whose
username matches that permalink. The exact detail must contain that same
permalink, one matching author link, and at most one visible root body in a
`div[dir="auto"]`. Nested cards, blockquotes, sidebars, hidden content, and
collapsed body text are not treated as the root body. A mismatch stops the
run. Missing text is `UNKNOWN`; visible but clipped or explicitly expandable
text is `PARTIAL`; the reader never fills in absent text.

The exact canonical manifest content is hashed into the existing checkpoint
query identity. Reusing a run ID with a changed query, feed, or detail returns
`CHECKPOINT_CONFLICT`; an identical completed replay does not add observations
or vacancies. This source currently accepts one cycle per CLI invocation. Use
the same run ID to verify restart/replay behavior.

The reader's semantic selectors are an offline implementation candidate. They
have not been verified against the live Threads UI. `BROWSER_UI` is never
assigned to synthetic observations, and this command does not test live search,
actor identity, session handling, platform permission, or third-party
visibility.

### PowerShell example

Run this from a private fixture directory outside the checkout. It builds one
synthetic recruiter post; only local HTML is served to Chromium.

```powershell
$private = Join-Path $env:TEMP ('threads-b1-ui-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $private | Out-Null
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$grant = '{0}:(OI)(CI)F' -f $who
icacls $private /inheritance:r /grant:r $grant '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
$dataRoot = Join-Path $private 'synthetic-root'
New-Item -ItemType Directory -Path $dataRoot | Out-Null
$account = 'test-b1-demo'
$actor = 'b1-demo'
$query = 'synthetic recruitment roles'
$post = 'https://threads.example.invalid/@synthetic-shop/post-001'
$feed = "<html><body><header role='banner'><a href='/@$actor'>Account</a></header><main><form role='search'><input type='search' value='$query'></form><article style='height:900px'><header><a href='/@synthetic-shop'>@synthetic-shop</a><a href='$post'>time</a></header><div dir='auto'>Visible synthetic post</div></article></main></body></html>"
$detail = "<html><body><header role='banner'><a href='/@$actor'>Account</a></header><main><article><header><a href='/@synthetic-shop'>@synthetic-shop</a><a href='$post'>time</a></header><div dir='auto'>Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng.</div></article></main></body></html>"
$manifest = @{
  schema_version = 1
  fixture_kind = 'SYNTHETIC_BROWSER_UI'
  query = $query
  cycles = @(@{ feed_html = $feed; detail_pages = @(@{ target = $post; html = $detail }) })
}
$fixtureFile = Join-Path $private 'ui-fixture.json'
[IO.File]::WriteAllText($fixtureFile, ($manifest | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
threads-local recruitment browser-watch --source ui-fixture --account $account `
  --fixture-file $fixtureFile --data-root $dataRoot --run-id b1-demo-001
```

The output contains only fixed status/reason codes and counts for batches,
candidate appearances, unique targets, duplicates, details, scrolls, blocked
requests, and watch/archive results. It does not print query, actor, permalink,
body, alias, or paths. Archive provenance stays `SYNTHETIC_FIXTURE`.

## Foreground and resume behavior

One cycle runs by default. `--max-cycles` accepts 1–5. `--max-posts-per-cycle`
accepts 1–20 and the whole fixture is checked before the engine starts. A
multi-cycle run requires a terminal and `--interval-minutes` of at least 15;
the interval is real foreground time. The watch is bounded and exits after the
requested number of cycles. Type `STOP` and press Enter, send EOF, or press
Ctrl+C while it waits. Other input lines, including blank lines, are ignored;
the wait continues until the remaining interval elapses. Ctrl+C returns status
130. There is no daemon, scheduler, missed-cycle catch-up, or hidden retry.
Restart with the same account alias, root, run ID, options, and fixture plan to
resume. A changed plan with the same run ID returns `CHECKPOINT_CONFLICT`; an
identical completed replay does not create a new observation. Reusing a local
fixture is not evidence of a fresh remote observation.

Terminal output contains only fixed statuses/reasons, counts, and the next due
time. It does not print aliases, UUIDs, paths, source URLs, fixture bodies, or
post/contact data. Fixture bytes and roots must remain outside the repository
and must never contain customer, applicant, account, credential, cookie, or
real contact data. BJ-03 retains the explicit policy of 30 days for raw text
and 90 days for derived vacancies.

## Windows PowerShell

Create a private directory for the fixture and a caller-owned synthetic data
root. Write fixture JSON as UTF-8 without a BOM:

```powershell
$private = Join-Path $env:TEMP ('threads-bj04b-private-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $private | Out-Null
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $private /inheritance:r /grant:r "${who}:(OI)(CI)(F)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
$dataRoot = Join-Path $private 'synthetic-root'
New-Item -ItemType Directory -Path $dataRoot | Out-Null
icacls $dataRoot /inheritance:r /grant:r "${who}:(OI)(CI)(F)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
$fixture = Join-Path $private 'fixture.json'
$fixtureJson = @'
{
  "schema_version": 1,
  "fixture_kind": "SYNTHETIC_ONLY",
  "cycles": [{
    "targets": ["https://threads.example.invalid/@synthetic-shop/post-001"],
    "posts": [{
      "url": "https://threads.example.invalid/@synthetic-shop/post-001",
      "body": "Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng.",
      "body_visibility": "FULL_VISIBLE",
      "published_at_utc": "2026-10-12T09:00:00Z"
    }]
  }]
}
'@
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($fixture, $fixtureJson, $utf8NoBom)
threads-local recruitment browser-watch --source fixture --account test-a `
  --data-root $dataRoot --fixture-file $fixture --run-id demo-001
```

For a bounded three-cycle foreground run, provide at least three fixture cycles
and add `--max-cycles 3 --interval-minutes 15`. Keep PowerShell paths quoted
when they contain spaces. All fixture timestamps must include `Z` or an explicit
zero UTC offset; the engine records its separate observation time in UTC.

## Ubuntu / POSIX shell

Create private directories with a restrictive umask. The fixture root and data
root parent must be outside the checkout:

```sh
umask 077
private_dir="$(mktemp -d)"
mkdir -m 700 "$private_dir/fixture-dir"
mkdir -m 700 "$private_dir/synthetic-root"
cat >"$private_dir/fixture-dir/fixture.json" <<'JSON'
{
  "schema_version": 1,
  "fixture_kind": "SYNTHETIC_ONLY",
  "cycles": [{
    "targets": ["https://threads.example.invalid/@synthetic-shop/post-001"],
    "posts": [{
      "url": "https://threads.example.invalid/@synthetic-shop/post-001",
      "body": "Cần tuyển 02 nhân viên bán hàng tại Q.7, TP.HCM; lương 8–12 triệu/tháng.",
      "body_visibility": "FULL_VISIBLE",
      "published_at_utc": "2026-10-12T09:00:00Z"
    }]
  }]
}
JSON
threads-local recruitment browser-watch --source fixture --account test-a \
  --data-root "$private_dir/synthetic-root" \
  --fixture-file "$private_dir/fixture-dir/fixture.json" --run-id demo-001
```

For the manual no-text result, run `threads-local recruitment browser-watch
--source manual`. It deliberately reads no URL or target file and reports
`UNKNOWN / NO_TEXT`.

All examples are synthetic and offline. They make no claim of collection,
consent, platform permission, third-party visibility, CRM delivery, or legal
clearance. No browser automation, real Threads access, account login, or real
contact handling is authorized by this CLI.
