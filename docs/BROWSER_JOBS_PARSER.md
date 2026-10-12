# BJ-01 Vietnamese Vacancy Parser

BJ-01 adds a deterministic, offline parser for caller-supplied Vietnamese text. Its implementation is [`recruitment_job_vacancy.py`](../src/threads_platform/standalone/recruitment_job_vacancy.py). The parser does not open Threads, make network requests, inspect accounts, persist observations, or authorize any browser capability. Browser reads and real contact processing remain gated by #64 and #103.

This is a bounded lexical parser, not a general Vietnamese language model. It recognizes only the phrases and field patterns listed below. Unrecognized or conflicting text stays `UNKNOWN` or `AMBIGUOUS`; the parser does not fill gaps from profile data, comments, images, outside knowledge, or inference.

## API and input

Call `extract_vietnamese_vacancies(observation, policy=ExtractionPolicy())` with a `PostObservation`. The input keeps target source, text provenance, completeness, and text separate:

| Input field | Meaning |
| --- | --- |
| `observation_id` | Local event identifier, 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`. |
| `account_uuid` | Canonical UUID string used only to scope the local identity. |
| `canonical_post_url` | Caller-supplied HTTPS target, at most 2,048 characters, without whitespace, credentials, or a fragment. This module validates the shape; it does not fetch or canonicalize it. |
| `source` | `SYNTHETIC_FIXTURE`, `MANUAL_URL`, or disabled `BROWSER_UI`. |
| `text_provenance` | `SYNTHETIC_FIXTURE_TEXT`, `OPERATOR_SUPPLIED_TEXT`, `BROWSER_UI_VISIBLE_TEXT`, or `NONE`. |
| `text_completeness` / `completeness_basis` | Completeness state and why the caller can make that claim. |
| `post_revision_id` | Required local ID when text is supplied; absent for URL-only input. |
| `observed_text` | Exact supplied text, retained privately in the input and never emitted in result reprs. |
| `text_truncated` | A clipped input must be marked `true` and `PARTIAL`. |
| `post_kind` | Only `ORIGINAL` is parsed. Reposts, quotes, replies, and unknown kinds remain unclassified. |

Accepted source/provenance combinations:

| Source | Provenance and completeness basis | Behavior |
| --- | --- | --- |
| `SYNTHETIC_FIXTURE` | `SYNTHETIC_FIXTURE_TEXT` + `FIXTURE_DECLARED` | Parse only synthetic text. Fixtures must use synthetic IDs, URLs, and contact values. |
| `MANUAL_URL` | `NONE` + `UNKNOWN`, with no text or revision | Return `UNKNOWN`; the URL proves no post content. |
| `MANUAL_URL` | `OPERATOR_SUPPLIED_TEXT` + `OPERATOR_ASSERTION` | Parse only the supplied text, offline. The parser does not verify the target or the completeness assertion. Contact fields remain blocked. |
| `BROWSER_UI` | Any | Reject with `BROWSER_SOURCE_DISABLED`; no browser provenance is minted. |

Mismatched provenance, browser-verified author/completeness claims on offline input, invalid IDs/targets, or missing text revision IDs return `REJECTED` with fixed reason codes. `COMPLETE` cannot be combined with `text_truncated=true`. Never mark text complete after clipping or because a URL was supplied.

## Output and evidence

`ExtractionResult` contains `status`, `classification`, `confidence`, `reason_codes`, zero or more `RecruitmentJobPostV1` records, and local identity references. Its repr reports only bounded counts and fixed codes. Record and observation reprs redact text, identifiers, raw values, contact values, and URLs.

| Result value | Meaning |
| --- | --- |
| `status` | `EXTRACTED`, `PARTIAL`, `NON_VACANCY`, `UNKNOWN`, or `REJECTED`. A field conflict or gated contact field makes a record/result `PARTIAL`. |
| `classification` | `RECRUITER_VACANCY`, `JOB_SEEKER`, `OTHER`, or `UNKNOWN`. This is separate from S7 intent classification. |
| `confidence` | Ordinal `HIGH`, `MEDIUM`, `LOW`, or `UNKNOWN`, not a probability. This version emits `HIGH`, `MEDIUM`, or `UNKNOWN`; `LOW` is reserved. Ambiguous fields, partial text, or unresolved duplicate roles prevent `HIGH`. |
| `reason_codes` | Stable enum values only; no source text or contact data. |

Every leaf is a typed `FieldEvidence[T]` with `state`, optional normalized `value`, optional exact `raw_value`, and zero or more `spans`:

- `SOURCE_SPAN` has direct text evidence. Spans are half-open Python Unicode code-point offsets into the exact original `observed_text`. Normalized values retain their original source spans.
- `ABSENT` means the field was not found in a supplied representation marked `COMPLETE`; it does not mean globally absent.
- `AMBIGUOUS` means the text is partial/unknown, conflicting, unsupported, or deliberately blocked. Competing source spans are retained when available. Unknown fields in partial input never become `ABSENT`.

The parser normalizes text for matching with NFC and case-folding while mapping matches back to the original text. Returned raw values preserve the caller's spelling, punctuation, line breaks, and code-point positions.

## Classification and recognized phrases

The current recruiter cues include `cần tuyển`, `đang tuyển`, `tuyển gấp`, `tuyển dụng`, `cần nhân sự`, `tìm nhân viên`, and `tìm ứng viên`, with no-diacritic forms. False-positive phrases such as `tuyển sinh`, `đội tuyển`, and `tuyển chọn` are excluded. Job-seeker cues include `em đang tìm việc`, `em xin việc`, `cần tìm việc`, `cần việc làm`, and stated applications such as `ứng tuyển vị trí`.

Recognized role families are sales/business sales, accounting, labor, retail collaborators, IT interns, IT, marketing, service, security, drivers, and generic staff/intern/collaborator titles. The parser records the matched title and a fixed category. It does not treat an employer/author label alone as a vacancy or infer a title from surrounding context.

Classification rules:

- Conflicting recruiter and job-seeker cues produce `UNKNOWN`.
- A job-seeker cue produces `JOB_SEEKER` and no vacancy records.
- Complete text with no recruiter cue produces `OTHER`; incomplete text without a recruiter cue remains `UNKNOWN`.
- A recruiter cue without a recognized role remains `UNKNOWN` with no vacancy record.
- More than the configured role-match bound returns `REJECTED` with `ROLE_LIMIT_EXCEEDED`.
- Unsupported post kinds return `UNKNOWN`. Reply/comment text is never merged into a parent post.

## Vacancy fields

Each record uses schema version `1` and contains:

| Field group | Extracted evidence |
| --- | --- |
| Identity | `post_identity_key=(account_uuid, canonical_post_url)`, caller-provided `post_revision_id`, `observation_id`, `vacancy_id`, `vacancy_revision_id`, and `role_anchor`. |
| Employer | Explicit labels `công ty:`, `nhà tuyển dụng:`, or `doanh nghiệp:` only. Author names are not substituted. |
| Role and headcount | Supported role phrase; an integer headcount only when directly stated immediately before the role. Counts for different titles are not combined. |
| Location | Explicit address label, district (`Q.7`/`quận 7`), and recognized city aliases such as Hà Nội, TP.HCM, Đà Nẵng, Bình Dương, Đồng Nai, and Cần Thơ. A district never implies a city. |
| Work and schedule | Explicit remote/onsite mode, shift, full-time, or part-time phrases. |
| Salary | Exact raw salary phrase; numeric bounds, explicit currency, period, and commission semantics only when supported. |
| Experience, requirements, benefits | Recognized experience phrase; text after `yêu cầu:`; and explicit benefit phrases or items after `phúc lợi:`. |
| Contacts | Phone, Zalo, email, and application URL are separate fields. Extraction is limited to synthetic fixture input; operator-supplied contacts remain `AMBIGUOUS` with no copied value. Phone and Zalo values require a verifiable placeholder such as `[SYNTHETIC_PHONE_001]` or `[FAKE_CONTACT_001]`; numeric values or malformed synthetic tokens following a phone/Zalo label remain `AMBIGUOUS` without returning the value, raw text, or source span, even in a synthetic fixture. Email domains must be `.invalid` or `.example`, and application URLs must use `.invalid`. No live contact use is authorized. |

When a post contains multiple recognized roles, field evidence is scoped by the role named in the same delimited clause. A clause that names multiple roles, or a field mention with no role reference, does not get copied to each vacancy: the affected field is `AMBIGUOUS` with no value. A field explicitly scoped to another role is not inherited by this role. This conservative rule applies to employer, location, work mode, schedule, salary, experience, requirements, benefits, and contacts.

Salary rules are intentionally narrow:

- `8–12tr/tháng` yields 8,000,000–12,000,000 with a monthly period. `tr` and `triệu` scale by 1,000,000; `k` scales by 1,000.
- A Vietnamese-looking amount does not imply VND. Currency is populated only when the text states `VND`, `VNĐ`, `đồng`, or `₫`.
- `25k/giờ` preserves its raw span, normalizes the amount to 25,000, and records `HOUR`; currency remains `ABSENT` in complete text unless explicitly stated.
- `lương thỏa thuận` is positive raw salary evidence. Its numeric minimum and maximum are `ABSENT` only for complete text; no numeric amount is invented.
- Conflicting salary values remain `AMBIGUOUS` and make the result `PARTIAL`. A percentage marker such as `%HH` is represented as `PERCENTAGE_UNSPECIFIED`; no commission rate is guessed.

Role IDs are deterministic local identifiers, not remote post IDs. A normal `vacancy_id` is derived from the account-scoped post identity and normalized role anchor, independent of source offsets and role order. If a post repeats the same title, unique stated city/district evidence can qualify each anchor; otherwise those duplicate roles remain ambiguous and do not receive invented IDs. Role anchors and field spans are tied to a supplied text revision. The parser does not store history, reconcile changes, mark a role filled, or merge a changed title into an earlier identity; BJ-03 owns later archive reconciliation.

## Bounds and failure behavior

`ExtractionPolicy` can lower, but not raise, these implementation limits:

| Limit | Value | Behavior |
| --- | ---: | --- |
| Text size | 10,000 Unicode code points | Exactly the limit is accepted. Larger input returns `PARTIAL`/`UNKNOWN` with `TEXT_LIMIT_EXCEEDED` and no records; the parser does not silently truncate it. |
| Matched roles | 20 | A policy may lower this bound. Exceeding it rejects the extraction. |
| Local IDs | 128 characters | Only the documented ASCII character set is accepted. |
| Target URL | 2,048 characters | HTTPS-shaped input only; no network or platform-specific URL verification occurs. |

An upstream source that clips text must set `text_truncated=true` and `text_completeness=PARTIAL`. The parser never upgrades partial or unknown completeness. `ExtractionPolicy` values outside the allowed ranges raise `ValueError("INVALID_POLICY")` before extraction.

## Synthetic example

```python
from threads_platform.standalone.recruitment_job_vacancy import (
    CompletenessBasis,
    ObservationSource,
    PostObservation,
    TextCompleteness,
    TextProvenance,
    extract_vietnamese_vacancies,
)

observation = PostObservation(
    observation_id="obs-synthetic-001",
    account_uuid="00000000-0000-4000-8000-000000000001",
    canonical_post_url="https://threads.example.invalid/@synthetic/post-001",
    source=ObservationSource.SYNTHETIC_FIXTURE,
    text_provenance=TextProvenance.SYNTHETIC_FIXTURE_TEXT,
    text_completeness=TextCompleteness.COMPLETE,
    completeness_basis=CompletenessBasis.FIXTURE_DECLARED,
    post_revision_id="rev-synthetic-001",
    observed_text="Cần tuyển 02 sales tại Q.7, TP.HCM; lương 8–12tr/tháng",
)
result = extract_vietnamese_vacancies(observation)
```

The input and its spans are private application data. Do not print the observation or field values, include real text/contacts in fixtures, or use this example as evidence of browser authorization, platform permission, or live extraction accuracy.
