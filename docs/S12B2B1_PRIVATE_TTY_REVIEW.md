# S12B-2B1 private TTY CRM review adapter

This slice adds an injected-stream review around the pure S12B-2A snapshot
contract. It has no CLI entry point and performs no file write, CRM request,
network call, browser action, or Threads action.

## Public interface

`threads_platform.standalone.recruitment_crm_review_tty` exposes:

```python
run_crm_review_on_private_tty(
    packet,
    queue,
    full_s4_records,
    exact_approvals,
    selected_opportunity_ids,
    destination_identifier,
    *,
    stdin,
    stdout,
    stderr,
    environment,
    now_utc,
) -> "SNAPSHOT_MATCH_ONLY"
```

All streams and the environment are explicit inputs. The helper does not read
`os.environ`, discover an account, load a local data root, acquire a lock, or
look up credentials. `now_utc` is caller-supplied, untrusted snapshot input;
the adapter does not provide a trusted clock.

## Review and confirmation

Before any source-derived value is displayed, all three streams must have
`isatty() is True`, and the supplied environment must have no nonempty known
CI flags such as `CI`, `GITHUB_ACTIONS`, `JENKINS_URL`, or `GITLAB_CI`. An
invalid gate writes nothing. The helper then:

1. Rebuilds the packet with the public S12B-2A builder and compares it with the
   supplied immutable packet.
2. Runs the public S12B-2A preflight with its generated required phrase as an
   internal snapshot check before rendering.
3. Displays only the selected items in their approved order: account UUID,
   opportunity ID, canonical target URL for private identity review, purpose,
   exact approved fields, per-item `TARGET_URL` opt-in, assertion labels, and
   opaque destination identifier.
4. Requests the exact `EXPORT <account UUID> <snapshot fingerprint>` phrase.
   Input is length bounded and must end as one complete line. EOF, interruption,
   an incorrect phrase, stream failure, or stale inputs fails closed.
5. Calls the public S12B-2A preflight again after input. The only successful
   return value is `SNAPSHOT_MATCH_ONLY`.

Target URLs are shown on the private review screen even when `TARGET_URL` is
not an approved export field. The screen labels this as identity review only;
display does not opt the URL into a handoff. Only the exact selected entries
are displayed. Control characters are escaped, while noncanonical Unicode,
surrogates, format controls, private-use characters, and values exceeding
render bounds are rejected. Output line count and total size are bounded.

The adapter never displays queue context, CVs, private notes, full S4 history,
or serialized handoff JSON. It does not write review output to stderr or logs.
The caller must keep private TTY output away from capture, shoulder surfing,
and public transcripts.

## Failure behavior

`CrmReviewTtyError` exposes only fixed codes. Snapshot failures are reported as
`CRM_REVIEW_TTY_STALE`; malformed confirmation is
`CRM_REVIEW_TTY_CONFIRMATION_MISMATCH`; noninteractive/CI, interrupted, unsafe
render, bounds, and I/O failures have separate fixed codes. Underlying
exception text, paths, targets, and private content are not included.

## Security limits and future work

This adapter does not authenticate the person using a terminal. An injected
stream returning `True` from `isatty()` is a test seam, not proof of a physical
private terminal or operator identity. The SHA-256 fingerprint is an equality
commitment, not a signature, legal consent, platform permission, or one-time
approval. Identical S12B-2A preflights can be replayed successfully; this slice
does not redeem or revoke approvals.

The inputs are caller-provided objects. The adapter does not acquire one
account lock, reread fresh S6/S4 state, or establish a trusted clock. State can
change while the helper waits for confirmation; the post-input equality check
detects changes only when the caller's supplied objects reflect them. A later
authorized integration must provide a one-lock fresh S6/S4 snapshot, trusted
clock, authenticated local operator boundary, durable approval redemption and
revocation, and a private no-clobber file sink with platform-specific path and
symlink protections. It must not hold the account lock while waiting for
terminal input. Downstream CRM retention, access, deletion, and withdrawal
obligations require separate review.

`SNAPSHOT_MATCH_ONLY` does not mean `APPROVED_TO_EXPORT`, `FILE_WRITE_READY`,
CRM delivery, publication, or any remote action. This source and its synthetic
fake-TTY tests do not authorize actual customer export or live Threads use.
