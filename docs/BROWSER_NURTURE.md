# Browser Nurture operator notes

Browser Nurture is a read-only, account-scoped browser workflow. It uses the registered account UUID, its existing Chromium profile, and the same account lock as one-shot browser commands. It does not use the official Threads API or the API Nurture runner.

## Current source gate

The live `browser` source remains `DISABLED_PERMISSION_PENDING`. Running it exits before reading the data root, opening a profile, or navigating. Synthetic Chromium tests do not enable live account access. Keep this gate in place until the repository's separate platform-permission and operator-UAT prerequisites are met.

The `ui-fixture` source is for offline development and CI. It accepts only a private fixture with the reserved `threads.example.invalid` origin, a synthetic account, and a synthetic expected username. The browser fulfills exact fixture pages locally and blocks unmapped requests. Do not place real account data, cookies, post text, contact details, or real URLs in a fixture.

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
- `browser_nurture_v1/checkpoints/<account-uuid>.json`

The versioned config contains the expected username, topics, relevance terms, work bound, and idle interval. The checkpoint contains bounded counters and SHA-256 fingerprints for deduplication; it does not store post text or raw post URLs. Existing account metadata, Nurture v1, browser profile, and recruitment state files are not migrated or rewritten.

CLI output contains fixed status codes and aggregate counts only. Keep private configuration and local state out of repository fixtures, logs, screenshots, and public CI output.

## Windows Task Scheduler

Use Task Scheduler only under the same interactive Windows user that owns the local account registry and Chromium profile. The live source remains disabled until its separate gate is cleared.

1. Create a task with an **At log on** trigger for that user.
2. Select **Run only when user is logged on**. Do not run it as `SYSTEM` or as a Windows service.
3. Add an action for the installed `threads-local.exe`. Set arguments to `browser-nurture run <alias> --source browser --continuous` and set **Start in** to the executable's installation directory.
4. In Settings, select **If the task is already running, do not start a new instance**. The account UUID lock also excludes one-shot browser commands and other runner instances.
5. Clear any duration limit that would forcibly terminate the process. Stop a foreground session with Ctrl+C so its active cycle can finish or cancel cleanly.

With the current source gate, this scheduled live command exits with `DISABLED_PERMISSION_PENDING`; it does not inspect the account or open Chromium. Do not replace it with `ui-fixture` for unattended operation. After a future gate change, verify the exact CLI help, private UAT prerequisites, and stop behavior before scheduling a live read.
