# Browser Nurture operator notes

Browser Nurture is a read-only, account-scoped workflow. It uses the registered account UUID, its existing Chromium profile, the shared account lock, and the visible UI reader. It does not use the official Threads API or the API Nurture runner.

## Status and boundaries

This update adds a supervised manual-assisted read. The operator opens Search and submits the first configured topic, then opens one result; the reader proceeds only when the visible Search region, query, same-card permalink and author, trusted main-frame click, and exact detail body provenance are verified. It uses the registered account UUID, existing Chromium profile, and account lock. Live UI compatibility remains **unverified**; Codex and CI use offline synthetic pages only.

The `browser` source requires an explicit source selection and a headed interactive one-shot confirmation. It reads only visible UI and stops on an account mismatch, login wall, challenge, rate limit, redirect, unsupported structure, timeout, or browser crash. The CLI does not expose likes, follows, replies, posts, or other engagement actions.

The `ui-fixture` source is for offline development and CI. It accepts only a private fixture with the reserved `threads.example.invalid` origin, a synthetic account, and a synthetic expected username. It fulfills exact fixture pages locally and blocks unmapped requests. Do not put real account data, cookies, post text, contact details, or real URLs in fixtures.

Topic-search metrics describe only visible posts returned by configured searches. They are labeled `search_*` in CLI output. They do not measure the home or For You recommendation feed and cannot establish that a recommendation changed.

## One supervised manual-assisted read without a cycle checkpoint

Run this separate command for one manual Search → post read:

```powershell
threads-local browser-nurture manual-assist <alias> --confirm-live-read-only
```

The command opens the selected account's headed Chromium profile under the existing account lock and verifies the configured actor on Home. In Chromium, manually open Search, submit the first configured topic, then press Enter in the terminal. The reader waits for at most eight seconds per attempt and continues only when the query input and URL match, a uniquely scoped Search region has a Top/Recent control, and visible result cards with one same-card permalink and author remain stable across polls. Cards from the underlying Home feed are outside that region and are ignored.

After the Search region is verified, manually open one of its results and press Enter in the terminal. The selected URL must come from a trusted main-frame click and match a result in the verified Search region. The detail reader then requires the exact permalink, matching author, and visible body to belong to the same post container. It never derives post text from a permalink. If Search or Detail cannot be proven, no post is counted as read.

The privacy-filtered V2 observation did not establish a Search-only region or visible result permalink: the observed Search surface had no `main`/`article` landmarks and no Search-labelled dialog. The manual-assist reader therefore keeps that shape fail-closed with `SEARCH_RESULTS_REGION_UNPROVEN`; it does not reuse Home content. The positive semantic Chromium fixture checks the reader contract only and is not evidence that the observed V2 Search surface is currently readable. A delegated Detail click without a captured permalink likewise reports `DETAIL_SELECTION_UNVERIFIED` and does not read the page body.

Recoverable UI errors print a fixed code while Chromium remains open and offer Retry, Skip, or Stop. Manual prompts expire after five minutes; error decisions expire after one minute. Browser crashes, account mismatch, challenge/rate-limit states, and origin-guard failures stop the run. The UI reader is read-only: the command does not like, follow, comment, post, or message. It prints only stages, safe status codes, counts, and classification; it does not print the topic, account, URL, post text, screenshot, or DOM.

This manual-assisted result is not an automated cycle. It does not read, create, or update the Browser Nurture cycle checkpoint, so output reports `checkpoint=UNCHANGED`. This command has Chromium offline regression coverage only; it has not been exercised against live Threads from Codex or CI.

## Separate automated checkpointed workflow

Configure the registered account alias, the expected username visible in the account banner, one or more search topics, relevance terms, a post bound, and an idle interval:

```powershell
threads-local browser-nurture configure <alias> `
  --expected-username <expected-username> `
  --topic <topic> `
  --topic <another-topic> `
  --relevance-term <term> `
  --max-posts-per-cycle 20 `
  --idle-seconds 900
```

Use the existing manually logged-in UUID profile for that alias. Do not copy the profile or paste cookies or tokens. The alias selects the local profile; only the visible UI actor check proves the signed-in account matches `--expected-username`.

For new profile onboarding, `threads-local account login <alias>` opens a headed browser in the selected account profile. Use the browser address bar to open `https://www.threads.com/`, then sign in manually if needed. The command never enters a password and does not restrict navigation during onboarding. It ends with `authentication=NOT_VERIFIED`; after Lead acceptance, use the manual-assisted pilot above to verify the configured actor.

Run one headed cycle from an interactive PowerShell console:

```powershell
threads-local browser-nurture run <alias> `
  --source browser `
  --confirm-live-read-only `
  --max-cycles 1
```

The live source uses the configured data root and does not accept `--data-root` or `--fixture-file`. It requires both interactive input and output, so it cannot be started accidentally from a scheduled or redirected one-shot command. The browser window remains visible. The runner verifies one visible actor marker before searching, reads a bounded number of visible search cards, opens only exact same-origin post pages, classifies full visible post text, and commits the checkpoint after the complete cycle.

The automated `run` command remains a separate checkpointed workflow and is not the manual-assisted pilot described here. This PR does not request a live run or authorize continuous mode. The reviewed manual-assisted command must be accepted before an operator runs it against a real Threads session.

## Live-use handoff

The manual-assisted command is the current supervised pilot. Do not use the older Home selector probe or automatic-cycle UAT checklist as acceptance evidence for this pilot. After Lead acceptance, use the manual-assisted command once and share only safe status codes and aggregate counts; keep private account evidence out of GitHub. The automated checkpoint and continuous workflows are separate and remain outside this pilot.

## Continuous mode and rollback — outside this pilot

Do not enable continuous mode or Task Scheduler as part of the manual-assisted pilot. A separate authorization is required before using the automated checkpointed workflow continuously.

After a successful supervised live cycle, review the visible account and aggregate result. From the same interactive user session, explicitly enable continuous operation for that account and current observation policy:

```powershell
threads-local browser-nurture enable-continuous <alias> `
  --confirm-supervised-read-only
```

This command requires an interactive console and a completed live checkpoint. The private, versioned opt-in is bound to the account UUID and current policy fingerprint. Changing topics, relevance terms, expected username, or the per-cycle post limit invalidates that opt-in. Stop an active foreground run with Ctrl+C first, then disable permission for later starts with:

```powershell
threads-local browser-nurture disable-continuous <alias>
```

Run continuous mode in the foreground with an explicit source and bounded work cycles between idle periods:

```powershell
threads-local browser-nurture run <alias> `
  --source browser `
  --continuous `
  --idle-seconds 900
```

`--max-cycles` can cap an invocation to at most five cycles. Each cycle is bounded to 180 seconds and at most 100 visible search candidates. Ctrl+C requests a safe stop after the active cycle reaches its commit boundary. A committed cycle is checkpointed once; an incomplete cycle is not committed. The runner attempts at most one browser restart per invocation. A later invocation reuses the same UUID profile and checkpoint. The account lock excludes another Nurture process and existing one-shot browser commands.

Configuration and private state are stored under the selected `THREADS_LOCAL_DATA_ROOT` (or the existing default root):

- `browser_nurture_v1/config/<account-uuid>.json`
- `browser_nurture_v2/checkpoints/<account-uuid>.json`
- `browser_nurture_v2/continuous_opt_in/<account-uuid>.json`

The configuration remains v1. The v2 checkpoint binds counters, topic rotation, and SHA-256 dedupe fingerprints to the observation policy. The separate v1 continuous opt-in stores only the account UUID, policy fingerprint, and enabled state. Existing v1 Nurture checkpoints remain untouched. Existing account metadata, browser profiles, operation journals, and recruitment state are not migrated or rewritten.

## Windows Task Scheduler

Use Task Scheduler only after the private supervised UAT and continuous opt-in above. Use the same interactive Windows user that owns the account registry and profile.

1. Create a task with an **At log on** trigger for that user.
2. Select **Run only when user is logged on**. Do not run as `SYSTEM` or as a Windows service.
3. Add an action for the installed `threads-local.exe`. Set arguments to `browser-nurture run <alias> --source browser --continuous` and set **Start in** to the executable's installation directory.
4. In Settings, select **If the task is already running, do not start a new instance**. The account UUID lock also excludes one-shot browser commands and other runner instances.
5. Clear any duration limit that would forcibly terminate the process. Stop a foreground session with Ctrl+C so its active cycle can reach the checkpoint boundary.
6. Under failure recovery, set **Restart every 30 minutes** and **Attempt to restart up to 1 time**. This gives a failed process one delayed retry.

Idle periods are held in process memory and restart from the configured interval after a process restart. Keep the delayed single-retry limit; repeated manual or scheduled restarts are not a reason to run extra browsing cycles.

Automated Chromium tests use temporary synthetic accounts and local request interception. They do not open a real Threads profile or make a remote Threads request. Report live UI compatibility only after the private Windows UAT above.
