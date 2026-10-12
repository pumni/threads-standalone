# S8: grounded reply drafting

S8 adds an offline, private-terminal draft flow over the S6 opportunity queue and
S7 qualification. It reuses `ReplyProposalGenerator`, its deterministic fake
implementation, bounded reply validation, and the existing edit/keep/discard
editorial model.

## Command

```powershell
threads-local recruitment opportunity-draft ALIAS --id OPPORTUNITY_UUID
```

Optional operator-confirmed job facts can be read from a local JSON file:

```powershell
threads-local recruitment opportunity-draft ALIAS --id OPPORTUNITY_UUID --facts-file PATH
```

The file is bounded to 8 KiB and must contain exactly these fields:

```json
{
  "provenance": "OPERATOR_ASSERTED_VERIFIED",
  "role": "Software Engineer",
  "location": "Remote - Vietnam",
  "pay_range": "USD 80k-95k per year",
  "eligibility": null,
  "verified_contact_available": true
}
```

The provenance value records the operator's assertion. The program does not
independently verify employer facts. Contact details themselves are not accepted
or copied into a reply; the boolean can support a question about whether the
operator should share the already verified contact details.

## Review behavior

The command reads one exact opportunity and its local S4 outcome. It ranks the
active S6 queue through S7 and drafts only when the selected item is an eligible
job seeker with complete operator-supplied context. Missing or partial context
shows `NEEDS_CONTEXT`; recruiter, ambiguous, skipped, duplicate, and S4-blocked
items do not reach the generator.

The terminal shows the account, exact target, source context and provenance,
bounded evidence excerpt, qualification reasons, and exact proposed reply. The
deterministic fake produces a clarifying question when role or pay facts are
missing. It does not invent an offer or repeat instructions embedded in source
text.

The operator can edit the one-line draft (up to 500 characters), approve it for
this local session, or discard it. Editing keeps the review pending; approval
closes that editor session. Session approval is not permission to send: the
output always remains `NOT_APPROVED_FOR_SENDING` and `NOT_SENT`. Drafts and edits
are not persisted and the S6 queue and S4 outcome journal are read-only in this
command. S9 owns the future exact context+draft sending approval contract.

All output is written to a private TTY. The command has no real AI provider,
provider credentials, network connection, browser startup, API call, or send
path. `ReplyProposalGenerator` remains an injectable fake-only test seam; the
CLI always uses `DeterministicFakeReplyGenerator`. Real provider configuration
and transmission remain outside this S8 slice under the separate #25 scope
decision.

## Limits

The offline templates are deterministic examples, not AI and not evidence of
reply quality. S7's language, intent, location, and job-family signals are
heuristic. `OPERATOR_ASSERTED_VERIFIED` is not independent verification, and
the assistant does not establish author identity or conversation coverage.
Quality flags are simple review advisories. No live Threads UAT was performed.
