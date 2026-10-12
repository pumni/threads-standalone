# BJ-03 private vacancy archive

BJ-03 adds RecruitmentJobStore, a strict local archive for synthetic Browser
Jobs observations and BJ-01 vacancy extraction results. It does not add a CLI,
browser reader, API client, CRM connection, or migration. Live Threads reads
remain NO_GO under #64/#103; real contact processing and production-root use
remain blocked pending separate coordinator and privacy decisions.

## Input and provenance

The only accepted write input is a BJ-02 BrowserJobObservationV1 with:

- source SYNTHETIC_FIXTURE, text provenance SYNTHETIC_FIXTURE_TEXT, and
  completeness basis FIXTURE_DECLARED;
- an exact HTTPS permalink on threads.example.invalid, with no query,
  fragment, credentials, or path traversal;
- the BJ-02 local observation ID and body-derived post_revision_id;
- an aware UTC observed_at_utc; an optional published_at_utc stays a
  separate source claim;
- an unverified synthetic author marker. The archive records only whether an
  author label was present and its UNVERIFIED status.

For text observations, callers provide the BJ-01 ExtractionResult produced
from to_parser_observation(observation). The store rebuilds the parser input
and checks the result against a fresh pure extraction before writing. A
missing-body observation can be recorded without a parser result. Quotes,
reposts, replies, image-only inputs, partial text, and unknown states keep their
source status; the store does not promote them to complete.

MANUAL_URL, OPERATOR_SUPPLIED_TEXT, BROWSER_UI, real Threads hosts,
unverified author labels, inconsistent timestamps, malformed source spans,
and mismatched parser results are rejected with fixed error codes and no
archive write. The persisted source-rights value is always
UNVERIFIED_SYNTHETIC_DECLARATION; it is not permission, consent, ownership,
or a lawful-basis claim.

## Account archive and identity

Construct the store with an explicit absolute Path and an explicit
RetentionPolicy. It reads no environment variable and has no implicit data
root:

    store = RecruitmentJobStore(
        data_root=synthetic_temp_root,
        retention_policy=RetentionPolicy(raw_text_days=30, derived_vacancy_days=90),
    )
    receipt = store.record_observation(
        browser_observation,
        extraction_result,
        now_utc=fake_utc_clock,
    )

Development and tests create a new synthetic root under a temporary directory.
They never open, copy, or inspect an operator's THREADS_LOCAL_DATA_ROOT.
This code does not authorize using that production root. Any future production
caller needs a separate coordinator GO and an approved rights/retention policy.

The namespace is job_vacancies_v1, with one bounded JSON archive per account
under accounts/<account-uuid>.json and a nonblocking account lock under
locks/<account-uuid>.lock. It is separate from opportunity_queue_v1,
opportunity_queue_v2, NurtureStore, and S4 journals.

Post identity is the exact pair (account_uuid, canonical_post_url). The
store preserves path case and does not guess URL normalization or remote IDs.
Each account archive keeps:

- immutable observation events with their observation IDs, observed and
  published timestamps, source/provenance, completeness, post kind, fixed
  parser results, and source-rights declaration;
- immutable body revisions, each with the exact synthetic text, content
  digest, parser output and source spans;
- stable vacancy IDs and immutable vacancy revisions, including per-field
  evidence states and spans into that exact body revision;
- current vacancy state. A role absent from a later complete original-post
  capture becomes NOT_REOBSERVED; partial, quoted, and unknown captures
  cannot assert absence.

Repeated observation IDs with identical event data are idempotent. Reusing an
observation ID with conflicting data fails closed. Unchanged text can share a
post_revision_id while every capture remains an independent event. Changed
text creates a new immutable post revision. Repeated revision IDs with
conflicting body or parser content fail closed. A later absence never means
filled or deleted.

## Bounds, retention, and deletion

Limits are hard caps and cannot be increased by a caller:

| Data | Maximum |
| --- | ---: |
| Post identities per account | 200 |
| Observation events per account | 200 |
| Observation events per post | 200 |
| Post revisions per post | 200 |
| Vacancy records per post revision | 20 |
| Vacancy revisions per account | 1,000 |
| Text per post revision | 10,000 Unicode code points |
| Account JSON archive | 12 MiB |
| Export fields | 32 |
| Export rows | 4,000 |

RetentionPolicy is mandatory, accepts a raw-text period of 1–30 days and a
derived-field period from the raw period through 90 days. These are maximum
synthetic test limits, not approved retention periods for real data.
apply_retention uses an injected aware UTC clock. Raw expiry clears the body,
source spans, raw field values, and exact permalink while retaining the
one-way identity digest and eligible derived non-contact values. Derived
expiry clears vacancy fields and marks the state EXPIRED. There is no automatic
migration or unbounded retention.

delete_post(account_uuid, canonical_post_url, now_utc=...) deletes only that
account's exact synthetic permalink record. It leaves a small tombstone with a
one-way identity digest and deletion timestamp, so the deleted record cannot
silently reappear. It does not touch S4 or any other namespace.

## Contact and export boundary

The parser's SYNTHETIC_FIXTURE label alone is not accepted as proof that a
contact is fake. Phone-like numbers and long numeric contact placeholders are
rejected before persistence. Phone/Zalo evidence can only be a bounded
[SYNTHETIC_PHONE_001], [SYNTHETIC_CONTACT_001], or
[FAKE_CONTACT_001] placeholder. Email domains must be .invalid or .example.
Text and application URLs must use a reserved synthetic .invalid domain.

The archive never stores a contact field's value, raw string, or source span.
It keeps only the evidence state, whether safe placeholder evidence existed,
and a bounded count. Generic status and repr output contain no post text,
URL, account UUID, author label, or contact.

For every non-contact field, evidence containing a phone-like value, approved
synthetic contact placeholder, email, URL, or contact label is stored as
AMBIGUOUS with no value, raw string, or source span. This keeps contact text
out of derived fields and exports even after the source body expires.

export_json and export_csv return local strings; they do not write to an
arbitrary path. Each call requires an account UUID, an explicit field allowlist,
confirmed=True, and an aware UTC clock. The allowlist excludes post text,
permalinks, author labels, spans, and all contacts. Any contact field request
or include_public_recruiter_contact=True fails with the fixed
VACANCY_STORE_CONTACT_EXPORT_BLOCKED reason. Real contact export needs a
separate approval and is not implemented here.

CSV cells that begin with =, +, -, or @, including after whitespace or
control characters, are prefixed with an apostrophe before the CSV writer
quotes fields. JSON export uses UTF-8-safe JSON encoding and has the same
allowlist and contact omission.

## Filesystem and failure behavior

The caller-supplied root must be absolute, existing, a regular directory,
outside the checkout, and free of symlink/reparse components. The namespace,
account and lock directories, lock file, archive and temporary files are
rechecked for safe type and containment. POSIX directories/files require
private mode bits. Windows protects the archive namespace ACL for the current
user, SYSTEM, and Administrators, then verifies inherited ACLs; missing
icacls, unexpected principals, or ACL uncertainty fail closed.

Writes use a same-directory temporary file, flush and fsync, then an atomic
hard-link for create or os.replace for update. A failed write leaves the
previous archive readable; an interrupted temporary file is ignored and never
treated as committed state. Each operation holds a nonblocking per-account
process lock. Corrupt JSON, duplicate keys, malformed UTF-8, wrong-account
archives, old/new schema versions, symlinks, oversized files, and path or
permission failures return fixed reason codes without source content or local
paths in the error.

## Synthetic verification

tests/test_recruitment_job_store.py covers the BJ-01 → BJ-02 → store bridge,
account isolation, exact-permalink dedupe, repeated and conflicting
observations, immutable revisions, partial/quote/image-only handling,
evidence spans, contact rejection and redaction including contact text embedded
in derived fields, export confirmation and invalid-field handling, CSV
injection, entry/text bounds, retention, deletion, locks, bad encodings/schema,
unsafe paths, interrupted writes, and disk/replace failures. All fixture
content is synthetic, all local data roots are newly created temporary paths,
and the store imports neither the Browser Jobs module nor any browser, network,
API, opportunity queue, or operation-journal module at runtime.
