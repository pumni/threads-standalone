# S10C offline content calendar export

S10C adds a deterministic iCalendar view over the accepted S10A local content
planner:

```text
threads-local recruitment content-calendar-export ALIAS
```

The command resolves the local account alias, takes the S10A account-scoped
calendar snapshot, and writes only UTF-8 iCalendar text to standard output. It
does not create an export file, make a network request, start a browser, invoke
an AI provider, transfer media, or publish content. The S10A store retains
responsibility for its account lock and strict local state validation.

## Exported events

The renderer accepts one `RecruitmentContentCalendarV1` snapshot with at most
40 retained packets. `REJECTED` packets are validated and then omitted. Each
remaining packet becomes one event. Event order is by scheduled UTC time and
then stable opaque UID, independent of input tuple order.

Events use UTC `DTSTART`, `DTEND`, and `DTSTAMP` values. iCalendar has
second-level date-time precision, so stored fractional seconds are truncated
for export. `DTEND` is 15 minutes after `DTSTART`; the event is transparent and
has no alarm. The calendar app can provide an operator-selected reminder.

`DRAFT` and `READY_FOR_MANUAL_PUBLICATION` use distinct fixed summaries and
generic descriptions. Every description includes `NOT_PUBLISHED_BY_PLANNER`
and states that the reminder does not authorize Threads publication. A
`READY_FOR_MANUAL_PUBLICATION` status is only an editorial decision in the
local planner. It never means the content was sent, accepted, or published.

Each `UID` is a domain-separated SHA-256 digest of the local account UUID and
packet UUID. This makes the identifier stable for the same packet and
account, and different across accounts, without including either raw UUID in
the calendar. The digest is an opaque correlation value, not a credential or
a substitute for access control.

Content lines use CRLF endings and are folded at the RFC5545 75-octet limit.
No source packet fields are rendered except the scheduled time, updated time,
and one of the two validated editorial states. In particular, the output never
contains post text, private notes, review notes, account or packet UUIDs,
URLs, media references, rights metadata, contact details, or personal
information.

## Failure behavior and limits

Malformed versions, statuses, account identities, timestamps, provenance,
rights assertions, duplicate packet identities, or publication status fail
closed with a fixed `CONTENT_PLANNER_STATE_INVALID` or
`CONTENT_PLANNER_ACCOUNT_MISMATCH` error. A snapshot over 40 packets returns
`CONTENT_PLANNER_LIMIT_REACHED`. Errors do not include packet content or local
paths. The renderer is pure and performs no filesystem or network operations.

The command prints the iCalendar document and no additional success message.
The operator decides whether and where to import or share it. Calendar files
reveal scheduled dates and times when shared, and calendar applications may
retain imported copies according to their own policies. Remove an imported
calendar using that application's controls when it is no longer needed.

This command does not install a scheduler, send reminders itself, create
alarms, publish or send Threads content, or establish platform permission.
Live Threads access and any automated publication remain outside this scope.
