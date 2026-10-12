# Browser Jobs acceptance contract (BJ-00)

**Status: proposed contract; coordinator acceptance is pending.** This is a documentation-only design for [BJ-00 #97](https://github.com/pumni/threads-standalone/issues/97), under [Browser Jobs #96](https://github.com/pumni/threads-standalone/issues/96). It does not authorize a browser read, contact collection, UI effect, release, or a later BJ slice.

Reviewed against the refreshed `origin/main@1db96e2ec2fcd7533f289ebfdc1693c3ce4291bb` on 2026-10-12. The current `AGENTS.md` requires a separate explicit handoff for each slice and keeps platform permission distinct from implementation permission.

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
| `post_identity_key` | Stable local identity for `(account_uuid, canonical_post_url)`. `post_kind` remains revisioned evidence, not a second post identity. |
| `post_revision_id` | Optional local opaque identity for an exact supplied-text revision; absent when no text was supplied. Repeated captures may share this revision while retaining separate observation IDs. |
| `canonical_post_url` | Exact canonical permalink for the item. Do not infer an API ID from it. Unknown hosts, redirects, or ambiguous targets stop processing. |
| `source` | Exactly `SYNTHETIC_FIXTURE`, `MANUAL_URL`, or `BROWSER_UI`. This says how the target entered the workflow; it does not prove that text or author data was observed. `BROWSER_UI` is valid only after authorized live UI evidence. |
| `observed_at_utc` | When this local observation was made. Keep distinct from any source-published timestamp. |
| `observed_author` | Optional author label plus its evidence status. A URL or operator-supplied text does not verify an author; keep unknown/unverified labels separate from verified visible attribution and from quoted/reposted attribution. A reader's author marker remains `UNVERIFIED`; a visible handle does not authenticate the account owner. |
| `post_kind` | `ORIGINAL`, `REPOST`, `QUOTE`, `REPLY`, or `UNKNOWN`, with any parent/quoted permalink and relationship recorded separately when available. |
| `observed_text` | Exact captured or operator-supplied text, private-only, optional, and potentially partial. Preserve its line breaks and characters so spans remain meaningful. |
| `text_provenance` | Exactly `OPERATOR_SUPPLIED_TEXT`, `SYNTHETIC_FIXTURE_TEXT`, `BROWSER_UI_VISIBLE_TEXT`, or `NONE`. This is independent of target `source`. |
| `text_completeness` | `COMPLETE`, `PARTIAL`, or `UNKNOWN`, inherited as input evidence, not asserted by the extractor. It describes only the supplied representation, not all Threads content. Reader `FULL_VISIBLE` maps to `COMPLETE` only when the full available visible body was captured without clipping. |
| `completeness_basis` | `OPERATOR_ASSERTION`, `FIXTURE_DECLARED`, `BROWSER_VISIBLE_VERIFIED`, or `UNKNOWN`. An operator assertion is not independently verified UI evidence. |
| `text_truncated` | True if the source or contract cap clipped the supplied text. A truncated observation must be `PARTIAL`, never `COMPLETE`. |
| `classification` | `RECRUITER_VACANCY`, `JOB_SEEKER`, `OTHER`, or `UNKNOWN`, plus ordinal confidence for the classification. |

Target source and text provenance are separate, and neither grants permission. Apply only these pairings:

| Target source | Allowed text provenance | Contract meaning |
| --- | --- | --- |
| `SYNTHETIC_FIXTURE` | `SYNTHETIC_FIXTURE_TEXT` | Synthetic text from a fixture with fixture-declared completeness; never live evidence. |
| `MANUAL_URL` | `NONE` or `OPERATOR_SUPPLIED_TEXT` | The URL is operator-supplied. URL-only input has no post text, author, salary, contact, or completeness evidence. Separately supplied text can be parsed offline, but its provenance remains operator supplied. |
| `BROWSER_UI` | `BROWSER_UI_VISIBLE_TEXT` | Only actual visible text from a separately authorized UI reader after the required source contract and private UAT are accepted. |

Reject mismatched source/provenance pairs. `MANUAL_URL` alone produces a URL-only `UNKNOWN` observation and no vacancy record. Do not derive text or metadata from the URL. For operator-supplied text, `COMPLETE` can mean only that the operator asserts the supplied text is complete; retain `completeness_basis=OPERATOR_ASSERTION` and never promote it to independently verified visible UI completeness. `COMPLETE` with `text_truncated=true` is invalid. `BROWSER_VISIBLE_VERIFIED` is allowed only when the exact target was opened and the full available visible body was captured under the reader contract. Hitting the 10,000-code-point cap, unresolved collapsed text, or any other clipping requires `PARTIAL`; never label cap-clipped text `FULL_VISIBLE` or `COMPLETE_VISIBLE`. Image-only input has no extractable text and remains `UNKNOWN` unless separately authorized evidence supplies text.

Opening a manually supplied URL or copying content from a live service remains subject to separately reviewed manual-use and data-rights rules. Do not capture cookies, full DOM, hidden fields, network traffic, private endpoints, or screenshots as an alternate evidence channel.

Use this vacancy-specific classification, independently of S7's intent classifier:

| Class | Meaning |
| --- | --- |
| `RECRUITER_VACANCY` | The post gives direct evidence that an employer/recruiter is advertising at least one available job for others. |
| `JOB_SEEKER` | The author is seeking work for themself or presenting their own job-seeking profile. |
| `OTHER` | The post is not a vacancy or job-seeking post. |
| `UNKNOWN` | Available text, attribution, or context cannot support a reliable class. |

Do not map S7's `RECRUITER` label directly to `RECRUITER_VACANCY`; the label alone does not prove a structured vacancy.

### `RecruitmentJobPostV1`

Create one versioned vacancy record for each distinct vacancy supported by a `PostObservation` classified `RECRUITER_VACANCY`. Keep post identity, observation/revision identity, and vacancy identity separate. One post can link to multiple vacancy identities; deduplicating the post must never collapse its roles. `JOB_SEEKER`, `OTHER`, and `UNKNOWN` observations do not create vacancy records. If role boundaries cannot be separated reliably, return an ambiguous observation with no vacancy records. If roles are separable but some fields are unclear, create separate conservatively partial records. Headcount counts positions within one role; it is not the number of vacancy records.

Each record contains these identity and classification fields, plus the vacancy fields below:

| Field | Proposed meaning |
| --- | --- |
| `schema_version` | Initial contract version `1`. |
| `post_identity_key` | Local post identity for `(account_uuid, canonical_post_url)`; `post_kind` is revisioned source evidence, not part of identity, and this is not a Threads/API ID. |
| `post_revision_id` | Local opaque identity for one distinct captured text revision of that post. Prior revisions remain immutable. |
| `observation_id` | The exact source observation supporting this vacancy. |
| `account_uuid` | The same private account scope as the observation. |
| `vacancy_id` | Stable local identity for one vacancy within the post, matched by its evidenced role anchor across revisions. |
| `vacancy_revision_id` | Immutable field snapshot for this vacancy in this post revision. |
| `classification` | `RECRUITER_VACANCY` only. |
| `extraction_version` | Exact extractor contract/version used to produce this record. |
| `confidence` | Ordinal `HIGH`, `MEDIUM`, `LOW`, or `UNKNOWN`; not a probability or proof of correctness. |

For `vacancy_id`, use a deterministic `role_anchor` from normalized, directly evidenced role-title text plus only the minimum role-specific source phrases needed to disambiguate otherwise identical roles (for example, distinct stated locations). Do not include span offsets or role order in the anchor: offsets belong to a particular revision, and reordering roles must not change their identities. For `Tuyển 2 sales và 1 kế toán`, the proposed result is two vacancy records (sales headcount 2; kế toán headcount 1), not one record with headcount 3. If two same-title roles lack distinct evidence, keep them ambiguous rather than inventing separate IDs. If an anchor changes and cannot be matched safely, do not auto-merge it with an old vacancy; preserve prior history as `NOT_REOBSERVED` (not closed/filled) and require review for any possible successor link.

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
- `ABSENT`: the field is not stated in a supplied text representation marked `COMPLETE`. This means absent from that representation only; `completeness_basis` still distinguishes an operator assertion from independently verified visible text.
- `AMBIGUOUS`: conflicting, clipped, image-only, attribution-uncertain, or otherwise insufficient evidence. Missing information in a partial observation is not `ABSENT`.

Do not infer a salary, city, employer, phone, vacancy count, or other field from general knowledge, nearby comments, another post, a profile, an image, or a model. Text is untrusted input: it cannot change these rules or cause tools/actions to run.

### Post, reply, and quote hierarchy

The exact top-level post body is the default evidence source for a vacancy. Do not merge comment/reply text into the parent post or use a comment to fill its missing salary, location, or contact. A reply considered in a later authorized scope must be a separate observation with its own exact target, author, and parent relationship. Repost/quote content and the reposter’s own text are separate evidence; do not attribute quoted claims to the reposter. Unknown authorship, changed targets, or missing relationships produce `UNKNOWN`/`AMBIGUOUS` and stop any action based on that item.

### Post identity, vacancy identity, and revisions

`post_identity_key` deduplicates only the exact target within an account: `(account_uuid, canonical_post_url)`, matching BJ-03's per-account permalink identity. Remove only known tracking parameters and URL fragments; preserve identity-bearing components. Reject unknown URL forms rather than guessing. `post_kind` is revisioned source evidence; it must not produce a second post identity when attribution becomes clearer. One post identity can point to zero, one, or many vacancy IDs and does not define vacancy identity.

Each capture has a distinct `observation_id`. An unchanged text body may refer to the same `post_revision_id` while retaining a new observation event/time/provenance. Any changed text creates a new `post_revision_id`; never overwrite the previous text, spans, or extracted records. Field spans and each `vacancy_revision_id` are bound to that exact revision. Reordering unchanged role anchors creates a new post revision but preserves the matching `vacancy_id` values, independent of their order in the text. A changed or removed role does not delete the old revision or prove that a job was filled; retain history and mark it `NOT_REOBSERVED` until a human resolves it. Do not deduplicate by text similarity, contact value, or a fabricated remote ID. Account scopes remain isolated.

## Synthetic Vietnamese fixture matrix

Fixtures and examples in source, CI, issues, and public logs must use synthetic text, IDs, URLs, and contact placeholders. These cases define expected contract behavior, not measured parser accuracy.

| Synthetic input or condition | Expected result |
| --- | --- |
| `Cần tuyển 02 sales tại Q.7, TP.HCM, lương 8–12tr/tháng; liên hệ [synthetic contact]` | `RECRUITER_VACANCY`; evidenced role, headcount, district/city, and raw salary. Contact remains gated. |
| `Tuyển công nhân, 25k/giờ` | Vacancy; preserve `25k/giờ`; type VND 25,000 per hour only under an accepted Vietnamese normalization rule for `k`; otherwise leave the amount ambiguous. |
| `Tuyển nhân viên; lương thỏa thuận` | Vacancy; the exact phrase `lương thỏa thuận` is positive raw salary evidence with a `source_span`; numeric lower/upper amounts are `ABSENT`, and no amount is invented. |
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

## Bounds and foreground CLI profiles

The canonical command name and safe source forms below align with [BJ-04B #102](https://github.com/pumni/threads-standalone/issues/102). Syntax remains illustrative until that issue's separate CLI ownership handoff. One invocation selects one account; the operator may have at most 1–2 separately authorized accounts configured.

```text
threads-local recruitment browser-watch --account test-a --source fixture \
  --fixture-file PRIVATE_SYNTHETIC_PATH --max-posts-per-cycle 20 \
  --interval-minutes 15

threads-local recruitment browser-watch --account test-a --source manual \
  --targets-file PRIVATE_TARGETS_PATH --interval-minutes 15
```

These command forms follow the illustrative customer-facing syntax in [BJ-04B #102](https://github.com/pumni/threads-standalone/issues/102); they describe the future foreground watch goal, not the initial one-cycle delivery below. `--source fixture` consumes local synthetic observations only. `--source manual` consumes operator-supplied target permalinks only; it never runs keyword search. A target file containing URLs does not provide post text, author, or UI completeness. URL-only targets remain `UNKNOWN` and produce no vacancy records unless a separately defined local input supplies text with `OPERATOR_SUPPLIED_TEXT` provenance. Neither mode opens Threads or makes network requests. Do not put post text or contacts in command arguments.

### Initial safe delivery: finite fixture/manual runs

The first BJ-04A/B delivery is a bounded offline/manual utility, not the customer's ongoing watch. Its source is limited to `fixture|manual`, with at most 20 fixture items or manual targets per cycle and no search query. It runs one cycle by default. Finalizing the #102 CLI grammar should add `--max-cycles` with a range of 1–5 for these local sources only; the default is 1. A configured interval (example: 15 minutes) applies only when more than one cycle is explicitly requested. The command runs in the foreground and exits at the cycle cap or on STOP. Repeating fixture/manual input never implies a fresh remote observation. The #102 three-cycle fake-source acceptance belongs to the later multi-cycle watch profile, not the initial one-cycle delivery.

The public browser/source limits below are reserved for a later, separately authorized browser-reader profile: at most 5 queries/batches, 100 candidates examined, 20 detailed post observations, and one 30-second reader batch timeout per cycle; at most 10,000 Unicode code points of text per post; and at most 200 active observations per account. Keep the existing timeout or use a stricter bound. If the text cap clips a body, set `text_truncated=true` and completeness to `PARTIAL`. Hitting a cycle item/query/time cap stops that cycle with a fixed `CAP_REACHED` status; it does not make otherwise complete individual observations partial. Never silently expand a bound.

### Future ongoing foreground watch: separate gated profile

The product goal in #102 is one command that can observe across multiple foreground cycles until STOP. This is a future profile, distinct from the finite initial delivery. It must use a configured interval of at least 15 minutes, the per-cycle query/candidate/post/time caps above, a single account lock, and an explicit session limit. Proposed CLI policy is `--max-session-minutes` default 60, hard maximum 240; STOP/Ctrl+C exits promptly. The session deadline bounds total cycles; there is no catch-up burst, daemon, scheduler, or background worker. These session values are proposals for coordinator acceptance, not enabled behavior.

Future `--source browser --keywords-file ...` remains `DISABLED_PERMISSION_PENDING` until #64 and #103 record narrow `GO_RESTRICTED` authority for the intended collection/use, the operator gives the required GO, #18/#46 fresh-root and test-account gates pass, #106's separately owned reader and bounded private UAT are accepted, and #102 grants its CLI integration/file lease. No issue or synthetic success enables that source. If a later authorized reader encounters a challenge, unknown actor, origin change, redirect, changed target, missing author, selector ambiguity, rate limit, timeout, or incomplete state, stop with a fixed safe status. Do not bypass, retry, or continue with another account. A later invocation must not silently replay/catch up an interrupted cycle.

## Capability and permission matrix

Implementation approval does not grant platform permission. Each capability is separate; a GO for reading never grants an effect.

| Capability | BJ-00 proposed status | Required evidence before enabling |
| --- | --- | --- |
| Synthetic fixtures and offline extraction | Allowed for synthetic-only development after its own handoff | Fake inputs/adapters, temporary synthetic roots, no Threads network. Synthetic success is not live evidence. |
| Operator-supplied permalink (`MANUAL_URL`) | Offline intake only; no application fetch implied | A manually supplied URL can be stored/processed locally only under an accepted purpose/retention policy. Manual navigation or capture is a separate capability. |
| Human manual UI navigation/capture | Not decided by this document | Verify applicable policy and data rights for the exact manual operation; do not infer permission from this issue or account ownership. |
| Automated search UI read | **NO_GO / disabled** | Written, current, express authorization for the intended use: automated search of public Vietnamese recruiter posts and the resulting structured vacancy/contact processing. Review the [Automated Data Collection Terms](https://www.facebook.com/legal/automated_data_collection_terms) and [Meta help terms](https://help.instagram.com/769983657850450); document authority, purpose/basis, account/query/data bounds, retention, and private root. Clear [#64](https://github.com/pumni/threads-standalone/issues/64) and [BJ-05 #103](https://github.com/pumni/threads-standalone/issues/103) with narrow `GO_RESTRICTED` evidence and operator GO; pass the [#18](https://github.com/pumni/threads-standalone/issues/18)/[#46](https://github.com/pumni/threads-standalone/issues/46) fresh-root/test-account gates; accept the separately owned [#106](https://github.com/pumni/threads-standalone/issues/106) reader and bounded private UAT; then obtain the separate #102 CLI lease and coordinator handoff. |
| Automated exact-post detail read | **NO_GO / disabled** | Same independent authorization and UAT, specifically covering post-detail text, attribution, disclosed contact fields, retention/export, and completeness claims. |
| Automated like/follow/repost | **Out of scope / disabled** | No unattended engagement. This contract supplies no gate or permission for these effects. |
| UI reply compose or Send | **NO_GO / disabled** | Separate interaction authorization and per-action design/UAT under [#67](https://github.com/pumni/threads-standalone/issues/67). Read permission is insufficient. |
| Official API reads or mutations | Outside Browser Jobs | Separate documented endpoint scopes and task authorization; no token/API fallback is part of the browser reader. See [API-AUTO #71](https://github.com/pumni/threads-standalone/issues/71). |

As checked for BJ-00, #64, #103, and #67 are OPEN and have no decision comments. No GO evidence was found. The Meta terms referenced by those gates must be reviewed and the exact decision recorded by the responsible coordinator; this document makes no legal interpretation of them. `BROWSER_UI` provenance must not be emitted until the applicable read gate and exact UAT have passed. Any permission expiry/revocation immediately returns the live reader to disabled state.

## Release states

| State | Meaning |
| --- | --- |
| `SYNTHETIC_READY` | A future implementation passes offline fake-source acceptance. It says nothing about live access, completeness on Threads, or permission. |
| `MANUAL_INPUT_READY` | A future implementation safely processes operator-supplied local text without fetching Threads. A URL alone is target intake, not text evidence, and cannot produce vacancy fields. This state does not authorize or automate manual UI activity. |
| `AUTHORIZED_BROWSER_READ_READY` | Only the exact read capability/account/purpose/bounds with documented authorization and private UAT is enabled. It is not a blanket permission for other reads, contact exports, or effects. |
| `NO_GO` | Permission is denied, missing, expired, or unclear; live browser access stays disabled. Offline synthetic/manual processing may continue only if independently accepted. |

**Current Browser Jobs gate:** `CONTRACT=PROPOSED`; `AUTOMATED_BROWSER_READ=NO_GO`; `CONTACT_EXTRACTION=BLOCKED`; `UI_EFFECTS=NO_GO`. A docs-only BJ-00 review does not make the product `SYNTHETIC_READY` or `MANUAL_INPUT_READY`.

## Slice boundaries and coordinator decisions

This contract covers BJ-00 only. BJ-01 is a pure offline extractor; BJ-02 is a synthetic bounded observation reader; BJ-03 is a separate private archive/export; BJ-04A/B adds a foreground engine and later CLI integration; BJ-05/#103 is the authorization gate; BJ-05B/#106 is separately permission-gated UI integration; BJ-06 is design-only for optional reviewed engagement; BJ-07 is release acceptance. Each requires its own explicit handoff. Do not edit existing queues, journals, S4/S6/S11A schemas, browser runtime, or shared CLI as part of this document. #92/S10B is merged; BJ-04B #102 still requires its own explicit shared-CLI integration/file lease.

Coordinator acceptance should explicitly decide:

1. Post/reply/quote hierarchy, classification meanings, field types, span semantics, and role-anchor rules for multiple vacancies/revisions.
2. Target source versus text provenance/completeness basis, including URL-only behavior.
3. Initial fixture/manual CLI grammar and finite cycle cap versus the future ongoing foreground watch/session budget.
4. Numeric query, item, cycle, text, archive, and timeout caps.
5. Contact purpose/basis, proposed retention periods, deletion behavior, and private export policy.
6. Whether BJ-00 is `ACCEPT` or `REQUEST_CHANGES`. BJ-01 must not start until an `ACCEPT` and a separate BJ-01 handoff.

## Related issues

- [Browser Jobs epic #96](https://github.com/pumni/threads-standalone/issues/96) and [BJ-00 #97](https://github.com/pumni/threads-standalone/issues/97)
- [Post-S5 roadmap #57](https://github.com/pumni/threads-standalone/issues/57), [S9 handoff #61](https://github.com/pumni/threads-standalone/issues/61), and [S10 content planner #62](https://github.com/pumni/threads-standalone/issues/62)
- [S11A synthetic reader #63](https://github.com/pumni/threads-standalone/issues/63), [S11B permission gate #64](https://github.com/pumni/threads-standalone/issues/64), and [S14 one-shot UI send gate #67](https://github.com/pumni/threads-standalone/issues/67)
- [BJ-01 extractor #98](https://github.com/pumni/threads-standalone/issues/98), [BJ-02 synthetic observation #99](https://github.com/pumni/threads-standalone/issues/99), and [BJ-03 private archive #100](https://github.com/pumni/threads-standalone/issues/100)
- [BJ-04A foreground engine #101](https://github.com/pumni/threads-standalone/issues/101), [BJ-04B CLI contract #102](https://github.com/pumni/threads-standalone/issues/102), and [BJ-05B gated UI reader #106](https://github.com/pumni/threads-standalone/issues/106)
- [Live UAT plan #46](https://github.com/pumni/threads-standalone/issues/46), [fresh-start gate #18](https://github.com/pumni/threads-standalone/issues/18), and [source rights gate #2](https://github.com/pumni/threads-standalone/issues/2)
- [API-AUTO plan #71](https://github.com/pumni/threads-standalone/issues/71), [S10B CLI #92](https://github.com/pumni/threads-standalone/issues/92), [S9B projection #95](https://github.com/pumni/threads-standalone/issues/95), and [BJ-05 hard gate #103](https://github.com/pumni/threads-standalone/issues/103)
- [Optional AI/CRM scope hold #25](https://github.com/pumni/threads-standalone/issues/25)
