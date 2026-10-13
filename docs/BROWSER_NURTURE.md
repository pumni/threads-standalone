# Browser Nurture operator notes

Browser Nurture is a read-only, account-scoped browser workflow. It uses the registered account UUID, its existing Chromium profile, and the same account lock as one-shot browser commands. It does not use the official Threads API or the API Nurture runner.

## Current source gate

The live `browser` source remains `DISABLED_PERMISSION_PENDING`. Running it exits before reading the data root, opening a profile, or navigating. Synthetic Chromium tests do not enable live account access. Keep this gate in place until the repository's separate platform-permission and operator-UAT prerequisites are met.

The `ui-fixture` source is for offline development and CI. It accepts only a private fixture with the reserved `threads.example.invalid` origin, a synthetic account, and a synthetic expected username. The browser fulfills exact fixture pages locally and blocks unmapped requests. Do not place real account data, cookies, post text, contact details, or real URLs in a fixture.

## Capability and measurement boundary

This feature is **SOURCE_READY**. Its required hosted acceptance is **OFFLINE_SYNTHETIC_E2E_VERIFIED** only when the Chromium-backed CLI tests pass in CI. Those tests do not access real Threads accounts or establish current Threads UI compatibility. The live source remains disabled, so this is not an available real-account or 24/7 nurture workflow.

Relevance counts describe only visible posts returned by the configured topic searches. Browser Nurture does not sample the home or For You recommendation feed, so these counts are not a recommendation-feed baseline.

## Configure and run

Configure a registered account with an expected UI username, one or more search topics, relevance terms, and a per-cycle post limit:

```powershell
threads-local browser-nurture configure <alias> `
  --expected-username <username> `
  --topic <topic> `
  --topic <another-topic> `
  --relevance-term <term> `
  --max-posts-per-cycle 20 `
  --idle-seconds 900
```

The expected username is checked against the visible signed-in account banner. The local alias is only used to find the account UUID and profile; it is not identity evidence.

The default one-shot command is:

```powershell
threads-local browser-nurture run <alias>
```

It currently returns the live-source disabled status without starting a browser. For a private synthetic fixture, use the explicit source and a fresh temporary data root:

```powershell
threads-local browser-nurture run <synthetic-alias> `
  --source ui-fixture `
  --fixture-file <synthetic-fixture.json> `
  --data-root <fresh-private-root> `
  --max-cycles 2
```

Foreground continuous mode uses the configured idle interval. `--max-cycles` can cap an invocation for a bounded run:

```powershell
threads-local browser-nurture run <synthetic-alias> `
  --source ui-fixture `
  --fixture-file <synthetic-fixture.json> `
  --data-root <fresh-private-root> `
  --continuous `
  --max-cycles 5 `
  --idle-seconds 900
```

Use Ctrl+C to stop a foreground run. The current cycle is committed only after its bounded reads finish; earlier committed cycles remain resumable. Browser process recovery is limited to one restart per invocation. A second crash stops the run and leaves the pending recovery marker for the next invocation. The runner never likes, follows, replies, posts, or catches up by replaying a completed cycle.

## Private state and output

Configuration and checkpoint files are stored under the selected `THREADS_LOCAL_DATA_ROOT` (or the existing default root):

- `browser_nurture_v1/config/<account-uuid>.json`
- `browser_nurture_v2/checkpoints/<account-uuid>.json`

The versioned config contains the expected username, topics, relevance terms, work bound, and idle interval. The v2 checkpoint binds its counters and SHA-256 dedupe fingerprints to a fingerprint of the expected username, ordered topics, relevance terms, and per-cycle post limit. Changing that observation policy starts a new checkpoint epoch, so prior URLs are re-assessed and prior counters are not mixed with new results. The first topic rotates after every committed cycle. Idle-interval changes retain the current epoch. The checkpoint does not store post text or raw post URLs. Configuration remains under `browser_nurture_v1/config`; new checkpoints are stored under `browser_nurture_v2/checkpoints`. Any existing v1 checkpoint file is left untouched and is not reused because it has no policy fingerprint. Existing account metadata, Nurture v1, browser profile, and recruitment state files are not migrated or rewritten.

CLI output contains fixed status codes and aggregate counts only. Keep private configuration and local state out of repository fixtures, logs, screenshots, and public CI output.

## Windows Task Scheduler

Use Task Scheduler only under the same interactive Windows user that owns the local account registry and Chromium profile. The live source remains disabled until its separate gate is cleared.

1. Create a task with an **At log on** trigger for that user.
2. Select **Run only when user is logged on**. Do not run it as `SYSTEM` or as a Windows service.
3. Add an action for the installed `threads-local.exe`. Set arguments to `browser-nurture run <alias> --source browser --continuous` and set **Start in** to the executable's installation directory.
4. In Settings, select **If the task is already running, do not start a new instance**. The account UUID lock also excludes one-shot browser commands and other runner instances.
5. Clear any duration limit that would forcibly terminate the process. Stop a foreground session with Ctrl+C so its active cycle can finish or cancel cleanly.
6. Under failure recovery, set **Restart every 30 minutes** and **Attempt to restart up to 1 time**. This gives a failed process one delayed retry.

The idle interval is held in process memory. A process restart begins a new invocation and does not inherit time spent idle before the failure. Keep the delayed, single-retry limit as a cooldown; repeated manual or scheduled restarts are not a reason to run extra browsing cycles.

With the current source gate, this scheduled live command exits with `DISABLED_PERMISSION_PENDING`; it does not inspect the account or open Chromium. Do not replace it with `ui-fixture` for unattended operation. After a future gate change, verify the exact CLI help, private UAT prerequisites, and stop behavior before scheduling a live read.
