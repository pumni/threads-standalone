# Browser Jobs foreground watch engine

BJ-04A adds a finite, synchronous coordinator for the already accepted BJ-01
parser, BJ-02 fake observation adapter, and BJ-03 private vacancy store. It is
an offline fixture engine. It does not add a command, a browser reader, a
network client, a scheduler, or a background worker.

## Source gates

`WatchSource.SYNTHETIC_FIXTURE` is the only source that can call the observer,
parser, or archive. The injected session must be an in-memory BJ-02 fake and
must return the reserved `https://threads.example.invalid` targets and fixture
metadata. Use synthetic UUIDs, author labels, query text, and post bodies only.

`WatchSource.MANUAL_URL` returns the fixed `UNKNOWN` / `NO_TEXT` result. The URL
is not opened, fetched, parsed, stored, or returned. A URL alone is not content
evidence.

`WatchSource.BROWSER_UI` immediately returns
`DISABLED_PERMISSION_PENDING`. That path does not inspect the fixture, start a
session, create the store or checkpoint directories, import Playwright, or
perform browser, HTTP, GraphQL, or account operations.

## Bounds and foreground behavior

- A run defaults to one cycle. `max_cycles` accepts only 1 through 5.
- Each cycle accepts at most 20 selected targets. The BJ-02 adapter further
  limits observation to 5 batches, 20 cards per batch, 100 appearances, 20
  exact detail reads, 10,000 Unicode code points per body, and 30 seconds of
  injected monotonic time.
- A multi-cycle run requires at least 900 simulated seconds between captures.
  The engine calls an injected sleeper and checks the stop signal around that
  wait. The engine itself never calls `time.sleep`.
- A failed, stopped, capped, redirected, challenged, login-walled, rate
  limited, or unclassified observation does not advance the completed-cycle
  checkpoint. There is no automatic retry or catch-up loop.
- `COMPLETE` describes a fully processed fixture cycle. Each post keeps its
  BJ-02 `COMPLETE`, `PARTIAL`, or `UNKNOWN` evidence state; incomplete text is
  never promoted.

The public seam is `BrowserJobWatchEngine.run(...)`. Synthetic runs require an
explicit account UUID, query, stable `run_id`, one `SyntheticCycleInput` per
requested cycle, injected UTC and monotonic clocks, an injected sleeper, a stop
signal, and a deterministic `capture_id_factory`. Invalid bounds are rejected
before the fake session is read or either store namespace is touched.

## Capture and archive flow

For each synthetic cycle, BJ-02 receives the unique capture ID and aware UTC
`observed_at_utc`. The engine passes only adapter observations to the pure
BJ-01 parser, then passes the original BJ-02 object and matching extraction to
BJ-03. Missing text is stored as `UNKNOWN` without invoking the parser. A new
observation of an unchanged post revision is counted as a duplicate finding,
not as a new vacancy.

The watch checkpoint is separate from BJ-03's `job_vacancies_v1` namespace:

```text
<caller-created synthetic root>/browser_job_watch_v1/
  accounts/<synthetic-account-uuid>.json
  locks/<synthetic-account-uuid>.lock
```

The root must already exist, be private, be outside the checkout, and be
created specifically for synthetic testing. Do not point the engine at an
operator data root or a customer archive.

Checkpoint schema v1 has a 16 KiB hard file limit, an exact key set, duplicate
JSON-key rejection, account binding, a query digest instead of query text,
capture-ID digests for reuse detection, a digest binding the selected targets
of a pending capture, and one optional pending capture. It
contains no post text, permalink, author label, contact, or filesystem path.
Unknown versions, corrupt data, unsafe paths, symlinks/reparse points, and
unverified permissions fail closed. There is no automatic migration or stale
file cleanup.

Before reading a fixture, the engine atomically records a **pending** capture
identity. This does not advance the completed-cycle cursor; it preserves the
same event identity if the process stops or fails. BJ-03's durable
`record_observation` result is required before the engine atomically marks the
cycle complete. If the archive write succeeds but the checkpoint update does
not, restart replays the pending capture with the same ID and timestamp. BJ-03
then returns its idempotent duplicate receipt, which is not reported as a new
vacancy.

Checkpoint access uses a separate nonblocking per-account lock. The engine
releases BJ-03's lock before acquiring the checkpoint lock; while holding the
checkpoint lock it calls BJ-03 methods that acquire and release the archive
lock. BJ-03 never acquires the watch lock, so the lock order has no reverse
edge.

## Result and privacy boundary

`WatchRunResult` exposes fixed status/reason codes, redacted counts
(`scanned`, `vacancy`, `duplicate`, `partial`, `errors`), completed-cycle
count, and an optional next due time. Its `repr` omits the next due timestamp.
It contains no account UUID, query, URL, capture ID, source text, contact,
actor label, local path, or underlying exception text.

## Synthetic verification coverage

`tests/test_browser_job_watch.py` exercises zero, one, 20, and 21 selected
targets; one, five, and six requested cycles; the five-batch appearance cap;
separate accounts; same and changed revisions; capture-ID reuse; first- and
second-cycle resume; stop during fake read, interval wait, and after archive
write; replay after archive/checkpoint interruption; atomic-write failure;
partial, quote, and image-only observations; UTC and monotonic clock rollback;
corrupt and unsupported checkpoints; private path, symlink, lock, and
permission failures; result redaction; and the disabled manual/browser gates.

The tests use an isolated synthetic basetemp. Windows may skip symlink
creation when the host disallows it; the store path checks remain fail-closed.
