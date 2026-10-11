# Recruitment session phases

`scripts/run_recruitment.py` is a foreground coordinator for the existing recruitment services. It accepts only local synthetic fixtures in examples and keeps operator-provided inbound settings ephemeral for one invocation.

## Phase behavior

| Phase | Remote access | Local writes | Effect |
| --- | --- | --- | --- |
| `--validate` | None | None | Checks the selected local account, preset, and any paired inbound options. It does not build an API client, create directories, acquire a lock, or write a receipt. |
| `--observe` | Bounded reads | Receipt and existing Nurture state | Discovers candidates and records a safe observation. It does not construct a mutation runtime. |
| `--review` | Bounded reads | Receipt and existing Nurture state | Produces a typed preview. With a reply or post file, exact target/candidate details are shown only on an interactive local terminal. |
| `--apply-reply-file` / `--apply-post-file` | Bounded revalidation reads | Receipt, existing Nurture state, mutation journal | Reviews the exact proposed effect, asks the operator to type `APPLY`, revalidates the same target or candidate, then allows one journaled effect. |

Review and apply require an interactive local terminal. Raw target context, source identifiers, reply text, and post text are sent only to terminal `stderr`; they are omitted from normal `stdout`, receipts, and command errors. Review and apply refuse when standard error is redirected or when `CI`/`GITHUB_ACTIONS` is set. Apply always performs a fresh review and explicit confirmation before mutation. A target or candidate mismatch stops the run without selecting a replacement.

## Inbound owner binding

Inbound discovery can be enabled for one alias and one invocation by supplying both `--inbound-owner-username` and `--inbound-thread-cap`:

```powershell
uv run python scripts/run_recruitment.py --account demo --validate --inbound-owner-username synthetic_owner --inbound-thread-cap 2
uv run python scripts/run_recruitment.py --account demo --observe --inbound-owner-username synthetic_owner --inbound-thread-cap 2
```

The cap is bounded to 1–10 owned threads. The username and cap are not saved in the preset, account file, receipt, or logs. Before reading the profile or conversations, the authenticated account identity must prove the configured username. An identity, profile, or conversation read failure is a failed run with a safe receipt outcome; it does not fall back to general discovery. When both options are omitted, the existing no-config discovery behavior remains available. Supplying only one option, an invalid username, or an invalid cap fails closed.

## Synthetic local review example

These commands document the local CLI shape. Only run them with synthetic fixtures and a newly created temporary root in a fake-API test environment; do not run them where operator credentials are available. The synthetic test suite substitutes a fake API and checks the terminal path:

```powershell
$env:THREADS_LOCAL_DATA_ROOT = "C:\Temp\synthetic-recruitment"
uv run python scripts/run_recruitment.py --account demo --review --review-reply-file synthetic-reply.json
uv run python scripts/run_recruitment.py --account demo --apply-reply-file synthetic-reply.json
```

The fixture must contain synthetic identifiers and text. Do not copy terminal previews into shell transcripts, CI logs, GitHub comments, or receipts. The same private review and confirmation rules apply to post candidates.

The foreground phases reuse the existing Nurture receipt/state and mutation journal. They do not change account, operation, Nurture, or other v1 schemas.
