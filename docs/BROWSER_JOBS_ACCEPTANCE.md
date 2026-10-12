# Browser Jobs acceptance contract (BJ-00)

**Status: proposed contract; coordinator acceptance is pending.** This is a documentation-only design for [BJ-00 #97](https://github.com/pumni/threads-standalone/issues/97), under [Browser Jobs #96](https://github.com/pumni/threads-standalone/issues/96). It does not authorize a browser read, contact collection, UI effect, release, or a later BJ slice.

Reviewed against `origin/main@bb8b01fce1cfe6601a21512223379be527814531` on 2026-10-12. The current `AGENTS.md` requires a separate explicit handoff for each slice and keeps platform permission distinct from implementation permission.

## Goal and boundary

Browser Jobs is a future, private, bounded workflow for identifying Vietnamese recruiter vacancy posts. It is a separate product from S7 job-seeker qualification and from the existing S6/S11A opportunity queues. A vacancy record must never be encoded as an S6/S11A queue item or an official API object.

The first usable mode is offline/manual input. The eventual command is foreground-only, operates on one explicitly selected account per invocation, has finite item/cycle/time caps, and exits on a stop signal or an unclassified platform state. It must not start a daemon, schedule work, or perform engagement. Live automated reads remain disabled until the independent permission gates below are cleared.

This contract does not change existing source, CLI behavior, browser runtime, account state, queues, or journals. It defines no migration and grants no right to collect or reuse platform data.

## Verified source baseline

- `BrowserSearchResultV1` and `BrowserConversationResultV1` are synthetic-only contracts. They carry at most 20 observations with excerpts capped at 500 characters; the search reader has existing bounds of 5 batches, 100 candidates, 20 results, a 120-character query, and a 30-second timeout. These excerpts are not a full-post archive contract.
- Existing S7 qualification distinguishes recruiter and job-seeker intent; it does not extract structured vacancy fields.
- Existing synthetic browser fixtures are not live UI evidence. A synthetic or manually supplied item must not be labeled `BROWSER_UI`, an official API target, or proof of complete post text.

See the source plans for [synthetic browser reading #63](https://github.com/pumni/threads-standalone/issues/63), [S7 qualification #59](https://github.com/pumni/threads-standalone/issues/59), and the [recruitment roadmap #57](https://github.com/pumni/threads-standalone/issues/57).

## Proposed record contract

### `PostObservation`

One observation describes one exact post-like source item. It records what was actually supplied or visibly observed, without asserting that the source is complete or authorized.

| Field | Proposed meaning |
| --- | --- |
| `schema_version` | Versioned observation contract; initial proposed value `1`. |
| `observation_id` | Local opaque identifier, not a Threads/API ID. |
| `account_uuid` | The local account scope used for the observation. Real values stay in the private local root; public examples use synthetic IDs only. |
| `canonical_post_url` | Exact canonical permalink for the item. Do not infer an API ID from it. Unknown hosts, redirects, or ambiguous targets stop processing. |
| `source` | Exactly `SYNTHETIC_FIXTURE`, `MANUAL_URL`, or `BROWSER_UI`. `BROWSER_UI` is valid only after authorized live UI evidence; `MANUAL_URL` means an operator supplied the URL and does not mean the application fetched it. |
| `observed_at_utc` | When this local observation was made. Keep distinct from any source-published timestamp. |
| `observed_author` | Author attribution as observed, kept private and separate from quoted/reposted attribution. Unknown attribution stays unknown. |
| `post_kind` | `ORIGINAL`, `REPOST`, `QUOTE`, `REPLY`, or `UNKNOWN`, with any parent/quoted permalink and relationship recorded separately when available. |
| `observed_text` | Exact captured or operator-supplied text, private-only, optional, and potentially partial. Preserve its line breaks and characters so spans remain meaningful. |
| `text_completeness` | `COMPLETE_VISIBLE`, `PARTIAL`, `IMAGE_ONLY`, or `UNKNOWN`. “Complete” means complete within the authorized visible source and contract cap, not complete Threads coverage. |
| `classification` | `RECRUITER_VACANCY`, `JOB_SEEKER`, `OTHER`, or `UNKNOWN`, plus ordinal confidence for the classification. |

The source label describes provenance, not permission. A manually supplied permalink may be processed offline; opening it or copying content from a live service remains subject to the separately reviewed manual-use and data-rights rules. Do not capture cookies, full DOM, hidden fields, network traffic, private endpoints, or screenshots as an alternate evidence channel.

Use this vacancy-specific classification, independently of S7's intent classifier:

| Class | Meaning |
| --- | --- |
| `RECRUITER_VACANCY` | The post gives direct evidence that an employer/recruiter is advertising at least one available job for others. |
| `JOB_SEEKER` | The author is seeking work for themself or presenting their own job-seeking profile. |
| `OTHER` | The post is not a vacancy or job-seeking post. |
| `UNKNOWN` | Available text, attribution, or context cannot support a reliable class. |

Do not map S7's `RECRUITER` label directly to `RECRUITER_VACANCY`; the label alone does not prove a structured vacancy.

### `RecruitmentJobPostV1`

Create one versioned vacancy record for each distinct vacancy supported by a `PostObservation` classified `RECRUITER_VACANCY`. Keep the observation and vacancy record separate. `JOB_SEEKER`, `OTHER`, and `UNKNOWN` observations do not create vacancy records. If one post advertises multiple jobs, create separate records only when their boundaries are evidenced; otherwise retain one partial/ambiguous result.

Each record contains these identity and classification fields, plus the vacancy fields below:

| Field | Proposed meaning |
| --- | --- |
| `schema_version` | Initial contract version `1`. |
| `vacancy_record_id` | Local opaque record ID; not a Threads/API ID. |
| `observation_id` | The exact source observation supporting this vacancy. |
| `account_uuid` | The same private account scope as the observation. |
| `classification` | `RECRUITER_VACANCY` only. |
| `extraction_version` | Exact extractor contract/version used to produce this record. |
| `confidence` | Ordinal `HIGH`, `MEDIUM`, `LOW`, or `UNKNOWN`; not a probability or proof of correctness. |

Store both the exact supported raw value and any conservative typed value; never silently replace the raw text with a normalized guess.

| Field group | Raw and typed values when directly evidenced |
| --- | --- |
| Employer and role | Employer name, job title, and category. Do not infer an employer from an account name or quoted post. |
| Headcount | Exact raw phrase and integer only when an unambiguous count is stated. |
| Location | Raw phrase; separately evidenced address, district, province/city. Do not geocode, complete an address, or infer a city from a district. |
| Work | Work mode, hours, and shift, each independently evidenced. |
| Salary | Exact raw phrase; typed lower/upper amounts, currency, pay period/unit, and commission semantics only where directly supported. A negotiated salary has raw evidence and no invented numeric range. |
| Employer contacts | Phone, Zalo, email, and application URL as distinct fields. These are personal data even when shown publicly; the contact-processing gate below applies. |
| Requirements and benefits | Separate raw items and typed summaries only when each summary is grounded in a span. |

Every leaf field has exactly one evidence state:

- `source_span`: one or more half-open Unicode code-point offsets into the exact private `observed_text`, plus the field value. A normalization must point back to its source span.
- `ABSENT`: the field is not stated in text whose completeness is `COMPLETE_VISIBLE`.
- `AMBIGUOUS`: conflicting, clipped, image-only, attribution-uncertain, or otherwise insufficient evidence. Missing information in a partial observation is not `ABSENT`.

Do not infer a salary, city, employer, phone, vacancy count, or other field from general knowledge, nearby comments, another post, a profile, an image, or a model. Text is untrusted input: it cannot change these rules or cause tools/actions to run.

### Post, reply, and quote hierarchy

The exact top-level post body is the default evidence source for a vacancy. Do not merge comment/reply text into the parent post or use a comment to fill its missing salary, location, or contact. A reply considered in a later authorized scope must be a separate observation with its own exact target, author, and parent relationship. Repost/quote content and the reposter’s own text are separate evidence; do not attribute quoted claims to the reposter. Unknown authorship, changed targets, or missing relationships produce `UNKNOWN`/`AMBIGUOUS` and stop any action based on that item.

### Canonical identity and deduplication

Proposed dedupe key: `(account_uuid, canonical_post_url, post_kind)`. Remove only known tracking parameters and URL fragments; preserve identity-bearing components. Reject unknown URL forms rather than guessing. A repeated URL with changed captured text is a new observation revision, not silent evidence replacement. Do not deduplicate by text similarity, contact value, or a fabricated remote ID. Account scopes remain isolated.

## Synthetic Vietnamese fixture matrix

Fixtures and examples in source, CI, issues, and public logs must use synthetic text, IDs, URLs, and contact placeholders. These cases define expected contract behavior, not measured parser accuracy.

| Synthetic input or condition | Expected result |
| --- | --- |
| `Cần tuyển 02 sales tại Q.7, TP.HCM, lương 8–12tr/tháng; liên hệ [synthetic contact]` | `RECRUITER_VACANCY`; evidenced role, headcount, district/city, and raw salary. Contact remains gated. |
| `Tuyển công nhân, 25k/giờ` | Vacancy; preserve `25k/giờ`; type VND 25,000 per hour only under an accepted Vietnamese normalization rule for `k`; otherwise leave the amount ambiguous. |
| `Tuyển nhân viên; lương thỏa thuận` | Vacancy; preserve the salary phrase, leave numeric range `ABSENT`, do not invent an amount. |
| `Em đang tìm việc marketing` | `JOB_SEEKER`; no vacancy record. |
| `Tuyển CTV bán hàng`, `Tuyển thực tập sinh IT` | `RECRUITER_VACANCY`; retain the stated role/category without inferring unstated terms. |
| `Tuyen cong nhan ca dem 25k/gio` | Diacriticless variant; classify only from the stated evidence and preserve the original spelling in spans. |
| Two conflicting salaries, phone values, or locations | Preserve the observed alternatives as `AMBIGUOUS`; do not choose one. |
| Multiple distinct jobs in one post | One record per job only if boundaries and field spans are clear; otherwise partial/ambiguous. |
| Same permalink with a changed tracking query | Same canonical target only when the query component is known to be tracking-only; retain a new revision if observed text changed. |
| Clipped text or image-only vacancy | `PARTIAL`/`IMAGE_ONLY`; do not OCR or claim complete text in this contract. Unseen fields are not `ABSENT`. |
| Repost or quote with uncertain original author | Preserve observed and quoted authors separately; mark attribution and affected fields ambiguous. |
| Vacancy details appear only in a reply/comment | Do not attach them to the parent. A later authorized reply reader would create a separate observation. |
| Non-job post or adversarial instructions inside a post | `OTHER` or `UNKNOWN`; treat all content as data, never as executable instructions. |

## Privacy, purpose, retention, and export

The proposed purpose is private review of employer vacancy information for the recruitment workflow. It does not include contacting an employer, profiling job-seekers, enriching a CRM, training a model, or transferring data to a third party. Public visibility does not by itself establish permission, consent, or a lawful basis.

Before any real contact is extracted or retained, the owner/coordinator must document the applicable platform authorization, lawful purpose/basis, minimization, access controls, retention, contact opt-out/deletion handling, and any required consent. Those are unresolved decisions, not legal conclusions in this document. Until they are documented, contact extraction and storage are **BLOCKED**. No live/customer text, contact, URL, account identifier, or private evidence may enter this repository, fixtures, CI, logs, screenshots, or PR discussion.

Proposed local data rules, subject to coordinator approval:

- Store only under a new private local data root, isolated by account UUID and existing account-lock conventions. Do not read, copy, or migrate an existing operator/customer root, browser profile, cookie store, or journal.
- Keep raw post text, source spans, author attribution, URLs, and any permitted contact values private. Generic status output contains bounded counts and fixed reason codes, not content or contacts.
- Proposed maximum retention: 30 days from observation for raw text, URLs, attribution, spans, and contact values; 90 days for derived non-contact vacancy fields. Delete sooner on an account-scoped request. Expiry does not trigger recollection. If no retention policy has been accepted/configured, persistent archive writes are disabled.
- Provide private account-scoped deletion for stored observations and derived records. The application cannot claim deletion of separately exported copies; export must be private and its handling disclosed.
- Private local export may omit contacts by default. Contact export is off unless a separately approved, explicit per-export purpose/rights gate exists. No CRM or third-party transfer is included in BJ-00.

These are conservative product proposals, not proof of legal compliance or permission. Coordinator/privacy review must accept or amend them before real data is used.

## Bounds and proposed one-command UX

Proposed initial limits: one selected account per invocation (at most 1–2 separately authorized accounts configured for the operator); one query per cycle; at most 5 search batches, 100 candidates examined, and 20 post observations per cycle; at most 10,000 Unicode code points of visible text per post; at most 5 cycles per invocation with a default of 1; and at most 200 active observations per account. Keep the existing 30-second read timeout unless a stricter bound is approved. Hitting any cap yields a bounded partial result and stops; it never expands the scope automatically.

Proposed eventual command grammar:

```text
threads-local browser-jobs watch ALIAS --source synthetic|manual --input FILE
    [--max-cycles 1] [--max-items 20]
```

`synthetic` and `manual` consume local fixtures or operator-supplied local input only; they do not open Threads or make network requests. Do not put post text or contacts in command arguments. The command runs in the foreground, handles a local stop signal, reports bounded status, and exits. No background worker, scheduler, autonomous action, automatic likes/replies, random canned replies, or unattended outreach is in scope. A `BROWSER_UI` source selector and any CLI registration require a later explicit task and shared-CLI ownership release; the grammar above is a proposal, not a CLI change in BJ-00.

If a later authorized reader encounters a challenge, unknown actor, origin change, redirect, changed target, missing author, selector ambiguity, rate limit, timeout, or incomplete state, stop and return a fixed safe status. Do not bypass, retry, or continue with another account. No catch-up or restart may replay an uncertain remote read/action.

## Capability and permission matrix

Implementation approval does not grant platform permission. Each capability is separate; a GO for reading never grants an effect.

| Capability | BJ-00 proposed status | Required evidence before enabling |
| --- | --- | --- |
| Synthetic fixtures and offline extraction | Allowed for synthetic-only development after its own handoff | Fake inputs/adapters, temporary synthetic roots, no Threads network. Synthetic success is not live evidence. |
| Operator-supplied permalink (`MANUAL_URL`) | Offline intake only; no application fetch implied | A manually supplied URL can be stored/processed locally only under an accepted purpose/retention policy. Manual navigation or capture is a separate capability. |
| Human manual UI navigation/capture | Not decided by this document | Verify applicable policy and data rights for the exact manual operation; do not infer permission from this issue or account ownership. |
| Automated search UI read | **NO_GO / disabled** | Written, current, express authorization for the intended use: automated search of public Vietnamese recruiter posts and the resulting structured vacancy/contact processing. Review the [Automated Data Collection Terms](https://www.facebook.com/legal/automated_data_collection_terms) and [Meta help terms](https://help.instagram.com/769983657850450); document authority, purpose/basis, account/query/data bounds, retention, private root, and bounded private UAT. Clear [#64](https://github.com/pumni/threads-standalone/issues/64) and [BJ-05 #103](https://github.com/pumni/threads-standalone/issues/103), then obtain separate coordinator handoff. |
| Automated exact-post detail read | **NO_GO / disabled** | Same independent authorization and UAT, specifically covering post-detail text, attribution, disclosed contact fields, retention/export, and completeness claims. |
| Automated like/follow/repost | **Out of scope / disabled** | No unattended engagement. This contract supplies no gate or permission for these effects. |
| UI reply compose or Send | **NO_GO / disabled** | Separate interaction authorization and per-action design/UAT under [#67](https://github.com/pumni/threads-standalone/issues/67). Read permission is insufficient. |
| Official API reads or mutations | Outside Browser Jobs | Separate documented endpoint scopes and task authorization; no token/API fallback is part of the browser reader. See [API-AUTO #71](https://github.com/pumni/threads-standalone/issues/71). |

As checked for BJ-00, #64, #103, and #67 are OPEN and have no decision comments. No GO evidence was found. The Meta terms referenced by those gates must be reviewed and the exact decision recorded by the responsible coordinator; this document makes no legal interpretation of them. `BROWSER_UI` provenance must not be emitted until the applicable read gate and exact UAT have passed. Any permission expiry/revocation immediately returns the live reader to disabled state.

## Release states

| State | Meaning |
| --- | --- |
| `SYNTHETIC_READY` | A future implementation passes offline fake-source acceptance. It says nothing about live access, completeness on Threads, or permission. |
| `MANUAL_INPUT_READY` | A future implementation safely processes operator-supplied local input without fetching Threads. It does not authorize or automate manual UI activity. |
| `AUTHORIZED_BROWSER_READ_READY` | Only the exact read capability/account/purpose/bounds with documented authorization and private UAT is enabled. It is not a blanket permission for other reads, contact exports, or effects. |
| `NO_GO` | Permission is denied, missing, expired, or unclear; live browser access stays disabled. Offline synthetic/manual processing may continue only if independently accepted. |

**Current Browser Jobs gate:** `CONTRACT=PROPOSED`; `AUTOMATED_BROWSER_READ=NO_GO`; `CONTACT_EXTRACTION=BLOCKED`; `UI_EFFECTS=NO_GO`. A docs-only BJ-00 review does not make the product `SYNTHETIC_READY` or `MANUAL_INPUT_READY`.

## Slice boundaries and coordinator decisions

This contract covers BJ-00 only. BJ-01 is a pure offline extractor; BJ-02 is a synthetic bounded observation reader; BJ-03 is a separate private archive/export; BJ-04A/B adds a foreground engine and later CLI integration; BJ-05/#103 is the authorization gate; BJ-06 is design-only for optional reviewed engagement; BJ-07 is release acceptance. Each requires its own explicit handoff. Do not edit existing queues, journals, S4/S6/S11A schemas, browser runtime, or shared CLI as part of this document. In particular, #92 owns a separate shared CLI change set.

Coordinator acceptance should explicitly decide:

1. Post/reply/quote hierarchy, classification meanings, field types, and span semantics.
2. Manual URL versus manual UI boundaries and the proposed command grammar.
3. Numeric query, item, cycle, text, archive, and timeout caps.
4. Contact purpose/basis, proposed retention periods, deletion behavior, and private export policy.
5. Whether BJ-00 is `ACCEPT` or `REQUEST_CHANGES`. BJ-01 must not start until an `ACCEPT` and a separate BJ-01 handoff.

## Related issues

- [Browser Jobs epic #96](https://github.com/pumni/threads-standalone/issues/96) and [BJ-00 #97](https://github.com/pumni/threads-standalone/issues/97)
- [Post-S5 roadmap #57](https://github.com/pumni/threads-standalone/issues/57), [S9 handoff #61](https://github.com/pumni/threads-standalone/issues/61), and [S10 content planner #62](https://github.com/pumni/threads-standalone/issues/62)
- [S11A synthetic reader #63](https://github.com/pumni/threads-standalone/issues/63), [S11B permission gate #64](https://github.com/pumni/threads-standalone/issues/64), and [S14 one-shot UI send gate #67](https://github.com/pumni/threads-standalone/issues/67)
- [Live UAT plan #46](https://github.com/pumni/threads-standalone/issues/46), [fresh-start gate #18](https://github.com/pumni/threads-standalone/issues/18), and [source rights gate #2](https://github.com/pumni/threads-standalone/issues/2)
- [API-AUTO plan #71](https://github.com/pumni/threads-standalone/issues/71), [S10B CLI ownership #92](https://github.com/pumni/threads-standalone/issues/92), [S9B projection #95](https://github.com/pumni/threads-standalone/issues/95), and [BJ-05 hard gate #103](https://github.com/pumni/threads-standalone/issues/103)
- [Optional AI/CRM scope hold #25](https://github.com/pumni/threads-standalone/issues/25)
