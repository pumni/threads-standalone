# Browser Nurture operator notes

Browser Nurture is a read-only, account-scoped workflow. It uses the registered account UUID, its existing Chromium profile, the shared account lock, and the visible UI reader. It does not use the official Threads API or the API Nurture runner.

## Status and boundaries

The owner reported a private Windows UAT on the verified `main@bb415886` baseline. The existing profile, authenticated Home session, and data-root ACL were confirmed, but the one-shot run stopped with `BROWSER_SELECTOR_DRIFT` before Home had visibly finished loading. A privacy-safe aggregate inspection of that rendered Home reported `ready=complete`, `mains=0`, and `bannerLinks=0`. This confirms the previous readiness and Home selectors did not cover that observed structure.

This update polls visible UI state for up to eight seconds per page operation. Home accepts one visible `main` root or one visible semantic `header` outside post cards. Its header fallback requires exactly one visible same-origin profile link with an accessible label beginning `Your profile`, `My profile`, `Your account`, or `My account`; the link must match the configured expected username. This generic semantic structure has only synthetic Chromium coverage. It is not a claim that the live Threads page uses that structure. Search and detail fallbacks remain bound to the exact visible query and exact post permalink, with unique author evidence. Stage-specific fixed errors include `HOME_UI_NOT_READY`, `HOME_ROOT_UNRECOGNIZED`, `HOME_ACTOR_UNVERIFIED`, `SEARCH_UI_NOT_READY`, `SEARCH_ROOT_UNRECOGNIZED`, `SEARCH_CARD_UNRECOGNIZED`, and `DETAIL_ROOT_UNRECOGNIZED`.

The corrected reader's compatibility with the owner's live session remains **unverified** until the supervised rerun below succeeds. Synthetic CI results do not establish current Threads UI compatibility.

The `browser` source requires an explicit source selection and a headed interactive one-shot confirmation. It reads only visible UI and stops on an account mismatch, login wall, challenge, rate limit, redirect, unsupported structure, timeout, or browser crash. The CLI does not expose likes, follows, replies, posts, or other engagement actions.

The `ui-fixture` source is for offline development and CI. It accepts only a private fixture with the reserved `threads.example.invalid` origin, a synthetic account, and a synthetic expected username. It fulfills exact fixture pages locally and blocks unmapped requests. Do not put real account data, cookies, post text, contact details, or real URLs in fixtures.

Topic-search metrics describe only visible posts returned by configured searches. They are labeled `search_*` in CLI output. They do not measure the home or For You recommendation feed and cannot establish that a recommendation changed.

For a manual before/after feed sample, use the same account and device and sample the first 20 visible posts from the same Home or For You tab at the same local time on three days before and three days after the observation period. Do not click, like, follow, reply, or open posts during the sample. Privately tally relevant, irrelevant, and unknown counts using the configured topics; retain aggregate counts and dates only, without post text, URLs, or actor details. Compare the six samples descriptively and do not attribute a difference to Nurture alone.

## Configure and run one supervised cycle

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

For new profile onboarding, `threads-local account login <alias>` opens a headed browser at Threads Home and waits for manual sign-in. It never enters a password. The command ends with `authentication=NOT_VERIFIED`; use the supervised Browser Nurture run below to verify the configured actor.

Run one headed cycle from an interactive PowerShell console:

```powershell
threads-local browser-nurture run <alias> `
  --source browser `
  --confirm-live-read-only `
  --max-cycles 1
```

The live source uses the configured data root and does not accept `--data-root` or `--fixture-file`. It requires both interactive input and output, so it cannot be started accidentally from a scheduled or redirected one-shot command. The browser window remains visible. The runner verifies one visible actor marker before searching, reads a bounded number of visible search cards, opens only exact same-origin post pages, classifies full visible post text, and commits the checkpoint after the complete cycle.

Output contains safe stage codes and aggregate counts such as `search_scanned` and `search_relevant`; it does not print post text, URLs, or account identifiers. For the next private Windows UAT, run this one-shot command once. A successful output has `status=COMPLETE source=BROWSER_UI`, `cycles=1`, and `checkpoint_cycles=1`; privately confirm the visible actor and that at least one visible search post opened at its exact detail page. Do not enable continuous mode or Task Scheduler as part of this rerun.

If the command stops with `HOME_ROOT_UNRECOGNIZED` or `HOME_ACTOR_UNVERIFIED`, inspect only aggregate structure in the authenticated Home tab. Open DevTools Console and run this snippet; enter the configured username only in the local prompt. The returned object contains counts only and no username, URL, text, DOM, storage, cookie, or network data:

```javascript
(() => {
  const expected = prompt("Configured username for local matching only")?.trim();
  const visible = (element) => {
    if (!element || element.hidden || element.getAttribute("aria-hidden") === "true") return false;
    let node = element;
    while (node) {
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0 ||
          node.getAttribute("aria-hidden") === "true") return false;
      node = node.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const inPost = (element) => element.closest('article, [role="article"]') !== null;
  const shellHeaders = [...document.querySelectorAll("header")].filter((header) =>
    visible(header) && !inPost(header) && !header.closest('main, [role="main"]')
  );
  const shellProfiles = shellHeaders.flatMap((header) =>
    [...header.querySelectorAll("a[href]")].filter(visible).filter((anchor) => {
      try {
        const url = new URL(anchor.getAttribute("href"), location.href);
        return url.origin === location.origin && !url.search && !url.hash &&
          /^\/@[A-Za-z0-9._:-]{1,96}$/.test(url.pathname);
      } catch { return false; }
    })
  );
  const selfShellProfiles = shellProfiles.filter((anchor) =>
    /^(your|my)\s+(profile|account)\b/i.test(anchor.getAttribute("aria-label") || "")
  );
  const expectedPath = expected ? `/@${expected}` : "";
  return {
    readyState: document.readyState,
    visibleMainRoots: [...document.querySelectorAll("main, [role=main]")].filter(visible).length,
    visibleBannerLinks: [...document.querySelectorAll('[role="banner"] a[href]')].filter(visible).length,
    visibleShellHeaders: shellHeaders.length,
    visibleShellProfileLinks: shellProfiles.length,
    selfLabelledShellProfileLinks: selfShellProfiles.length,
    expectedActorShellLinks: selfShellProfiles.filter((anchor) => {
      try { return new URL(anchor.getAttribute("href"), location.href).pathname === expectedPath; }
      catch { return false; }
    }).length
  };
})();
```

Share only the stage code and these aggregate counts privately. Do not paste a screenshot, URL, profile path, actor value, post content, or raw page markup into an issue or PR. If the expected actor count is not exactly one in a recognized visible structure, the reader must continue to fail closed until a safe visible marker can be established.

## Private Windows UAT checklist

Run this locally with the operator-selected account and the limits in its private configuration. Do not paste output that contains private paths or account evidence into GitHub.

1. Run the one-shot command above. Confirm the visible account control shows the configured username. Let the bounded cycle complete and privately record its status, aggregate search counts, and `checkpoint_cycles=1`.
2. Repeat the same one-shot command. Confirm the checkpoint resumes and previously read posts are deduplicated (`search_scanned` should be zero when the same results remain visible).
3. To verify account mismatch handling, temporarily configure a deliberately different expected username, then run one supervised cycle. Confirm it stops with `BROWSER_SESSION_MISMATCH` before issuing a topic search. Restore the correct username afterward; changing configuration resets the policy-bound checkpoint epoch.
4. Press Ctrl+C during a continuous foreground run. The current bounded cycle may finish and commit; the runner then reports `STOPPED`. Restart one-shot and confirm committed posts are not replayed.
5. Redact the alias, expected username, local paths, screenshots, URLs, post text, and all profile evidence before sharing. Share only the status code and aggregate counts.

This checklist is an operator action. Source code and offline E2E tests do not mark private UAT complete.

## Continuous mode and rollback — separate authorization required

Do not enable continuous mode or Task Scheduler during the selector-drift UAT. The owner or Lead must authorize that step separately after reviewing the corrected one-shot result.

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
