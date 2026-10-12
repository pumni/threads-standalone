# Browser Jobs foreground CLI (BJ-04B)

This slice is a **`SYNTHETIC_READY` implementation candidate**. It runs the
BJ-04A foreground engine only with a private local fixture. It is not a live
Threads reader, customer delivery, or production automation claim.

The installed command is:

```text
threads-local recruitment browser-watch
```

It supports two explicit source values:

- `fixture` validates a bounded UTF-8 fixture, maps it to BJ-02 fake session
  objects in memory, then calls the unchanged BJ-04A engine.
- `manual` returns `UNKNOWN / NO_TEXT`. This initial CLI does not accept or
  open a target URL/file, parse a post, or write an archive for manual input.

There is no `browser` source in this command. Actual browser reads remain
`DISABLED_PERMISSION_PENDING` under #64 and #103. The command has no API,
Playwright, HTTP, keyword search, account-store, CRM, post, reply, like, follow,
DM, or background-service path.

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

`--account` accepts only aliases beginning with `test-` or `synthetic-`. A
stable UUID is derived from that alias; the CLI never reads or creates a real
`LocalAccountStore` entry. Use a new alias per synthetic account. The required
`--data-root` is a caller-created, empty, private synthetic directory outside
the checkout. The CLI never creates or overwrites the root. On first use it
adds a private marker only to that empty directory; later resumes may reuse the
marked root. An unmarked nonempty root or unexpected top-level file is rejected.
Its only top-level store directories are `browser_job_watch_v1` and
`job_vacancies_v1`.

## Foreground and resume behavior

One cycle runs by default. `--max-cycles` accepts 1–5. `--max-posts-per-cycle`
accepts 1–20 and the whole fixture is checked before the engine starts. A
multi-cycle run requires a terminal and `--interval-minutes` of at least 15;
the interval is real foreground time. The watch is bounded and exits after the
requested number of cycles. Type `STOP` and press Enter, send EOF, or press
Ctrl+C while it waits. Ctrl+C returns status 130. There is no daemon, scheduler,
missed-cycle catch-up, or hidden retry. Restart with the same account alias,
root, run ID, options, and fixture plan to resume. A changed plan with the same
run ID returns `CHECKPOINT_CONFLICT`; an identical completed replay does not
create a new observation. Reusing a local fixture is not evidence of a fresh
remote observation.

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
