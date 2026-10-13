# BJ-06: relevance-first engagement review

**Decision: DESIGN_READY_FOR_MANUAL_HUMAN_REVIEW_ONLY**

**Scope:** Issue #104 under Browser Jobs epic #96. This is a policy and architecture document only. It does not implement or authorize a browser action, platform read, like, reply, composer entry, submit, or any other remote effect. The contract and examples below are inert design material, not executable approval.

The requested random likes and occasional replies from a sample-answer file are not accepted. The current Browser Jobs watch path remains read/archive-only. It cannot execute engagement, and this work adds no runtime command, flag, dependency, data store, or behavior.

## Capability decisions

| Capability | Outcome | Boundary |
| --- | --- | --- |
| OBSERVE_SYNTHETIC_ONLY | Accepted for internal fixture evidence | Fixtures remain synthetic; this does not establish live account, post, author, or platform evidence. |
| REVIEW_LIKE | Proposal/manual-only design | A relevance suggestion may be reviewed per target. The software does not click Like. |
| REVIEW_REPLY | Proposal/manual-only design | A context-bound draft may be shown as a manual suggestion after separate authorization. The software does not compose or send it. |
| AUTOMATED_LIKE | NO_GO | No background, scheduled, probabilistic, bulk, or watch-loop likes. |
| AUTOMATED_REPLY | NO_GO | No unattended, canned, random, bulk, or watch-loop replies. |
| UI_REPLY_COMPOSE | NO_GO | No automated entry into a reply composer. |
| UI_SUBMIT | NO_GO | No automated submission or send attempt. |

A project owner’s development approval is not evidence of Meta/platform permission to cause remote effects. Read permission and effect permission are separate. No capability in this table becomes available because a post is public, a template exists, or a reviewer previously approved another target.

## Inert proposal contract

This is a conceptual review checklist, not a data model, API, or implementation request. Do not persist or publish real account identifiers, target URLs, post text, people’s names, or contact values in examples, logs, fixtures, or audit output.

| Review field | Required meaning |
| --- | --- |
| Proposal kind | One of REVIEW_LIKE or REVIEW_REPLY; this records a suggestion category and never invokes an effect. |
| Account discriminator | Synthetic namespace marker for internal fixtures. Any future live account binding would need a separately authorized, verified account context; no actual account identifier belongs in this document or public evidence. |
| Exact target identity | The exact observed and approved target permalink identity, bound to the account and context. A changed, redirected, or ambiguous target invalidates the proposal. Examples use only synthetic references, never a real URL. |
| Source provenance | Preserve the distinction among SYNTHETIC_FIXTURE, MANUAL_URL, and any separately authorized browser evidence. A fixture can never be relabelled as real BROWSER_UI evidence. |
| Context completeness | One of FULL_VISIBLE, PARTIAL, or UNKNOWN, with its evidence basis. UNKNOWN cannot support a contextual reply. PARTIAL must remain visibly partial and cannot support claims beyond what is actually present. |
| Vacancy and relevance | A bounded vacancy/relevance class plus the exact visible role and region cues that support the suggestion. Missing or unrelated context yields NO_PROPOSAL. |
| Rationale and risk flags | A short, reviewable explanation and fixed risk flags, including insufficient context, missing compensation details, unverifiable claims, repetitive wording, sensitive data, and target/context mismatch. |
| Draft binding | For replies, the operator-authored draft or template version and its content digest. The draft must be displayed in full for review; a digest is not a substitute for seeing the text. No applicant identity, experience, salary, availability, or contact claim may be invented. |
| Expiry and context binding | Expiry plus a binding to the exact account, target, observed context, provenance, and draft version. Any change or expiry invalidates the proposal and requires a fresh review. |
| Per-target human decision | A person previews and explicitly approves or rejects this exact proposal for this exact target. Approval is single-target and single-context; it cannot be inherited by another post or inferred from a general setting. |
| Fixed, redacted status | A small status such as PROPOSED, MANUAL_SUGGESTION_ONLY, REJECTED, EXPIRED, or STOPPED. Statuses contain no target URL, account ID, names, contact details, post text, or credentials. There is no SENT status in this proposal contract. |
| Audit evidence | A future separately approved workflow records the proposal and draft versions, review decision, time, and fixed outcome/reason codes in its protected local audit. Public CI, issue, and PR evidence remains synthetic and redacted; it contains no actual account/target values or reply text. |
| Interaction limits | One target at a time; no bulk operation, random selection, probability, repeated template spraying, automatic retries, or watch-loop execution. These are design constraints, not implemented controls. |

A MANUAL_URL with no supplied text remains UNKNOWN / NO_TEXT: the URL alone says nothing about the post body, author, role, salary, region, or completeness. It cannot justify a contextual reply or metadata inferred from the URL. Do not invent text or fill gaps with a sample answer. A SYNTHETIC_FIXTURE remains synthetic throughout every review and report.

For REVIEW_REPLY, insufficient context, unknown provenance, a stale target, or a material target/context mismatch means NO_PROPOSAL. A partial observation can at most support a clearly qualified request for missing details when the visible portion itself supports that request; it cannot support factual claims about omitted content. REVIEW_LIKE also needs an explicit, visible relevance rationale and per-target human review. Neither review outcome means that a like or reply was performed.

## Synthetic Vietnamese illustrations

These examples are fictional, contain no personal identity or contact data, and are suggestions for human review only. They are not actions, templates approved for reuse, or evidence about any real post.

1. **Kế toán tổng hợp — miền Nam.** Synthetic context says the role includes month-end reconciliation and asks for prior accounting experience. Possible suggestion: “Bài đăng đã nêu nghiệp vụ đối chiếu cuối tháng. Có thể bổ sung phần mềm kế toán và phạm vi chứng từ để người quan tâm tự đánh giá mức phù hợp.” The suggestion refers only to stated duties and does not claim an applicant’s experience.
2. **Frontend React/TypeScript — miền Bắc.** Synthetic context names React and TypeScript and describes a frontend role. Possible suggestion: “Để người quan tâm đối chiếu yêu cầu, có thể nêu rõ phạm vi sử dụng React/TypeScript và các bước phỏng vấn.” No candidate identity or skill claim is supplied.
3. **Nhân viên kho, ca xoay — miền Trung.** Synthetic context states warehouse duties and rotating shifts but gives no exact working area or shift hours. Possible suggestion: “Bài đăng có nhắc ca xoay; có thể bổ sung khung giờ và khu vực làm việc để người quan tâm tự đánh giá lịch phù hợp.” The suggestion asks for details instead of guessing them.
4. **Nhân viên vận hành — miền Nam, thiếu mức lương.** Synthetic context describes the work but omits compensation. Possible suggestion: “Để người quan tâm cân nhắc đầy đủ, anh/chị có thể cho biết khoảng lương dự kiến và chế độ hoặc lịch ca áp dụng không?” No salary, benefit, or promise is invented.
5. **Thiếu ngữ cảnh — NO_PROPOSAL.** Synthetic text only says “đang tuyển, việc tốt, nhắn tin để biết thêm,” without a role, duties, region, or other relevant details. Result: NO_PROPOSAL, reason INSUFFICIENT_CONTEXT; do not draft a reply.

Reject these patterns:

- An unrelated “inbox mình” or other generic invitation that does not address the vacancy.
- Repeating emoji, “ib,” “up,” or stock wording across unrelated posts.
- Impersonating an applicant, employer, recruiter, or other person.
- Inventing applicant identity, experience, salary expectations, salary ranges, benefits, availability, or contact details.
- Blind likes, random reply chances, probability loops, bulk actions, and repeated unsolicited comments.

## Manual-only future path

There is no live path in this change. A future workflow may present a copyable manual suggestion outside the watch loop only when the operator-supplied context may independently be used for that purpose; it must not open a composer or send. Missing, stale, or unauthorized context means NO_PROPOSAL. If effect permission is absent, a copyable suggestion does not become an effect approval. No sentence here authorizes automated collection or platform reading.

Before any future human-authored reply, the reviewer must see the exact text, verify that the speaker is represented truthfully, and satisfy any applicable disclosure requirement. A proposal must not impersonate an applicant, employer, recruiter, or other person.

Any future one-shot UI path would require its own independently verified platform and effect rights and a separately accepted #67 implementation. Before any effect, that future design would have to:

1. Re-verify the exact account, target, visible context, and displayed final draft for this single proposal.
2. Require a fresh, explicit human confirmation for that exact account-target-draft binding; no standing or batch approval.
3. Durably journal an unresolved attempt, such as SUBMIT_ATTEMPT_UNRESOLVED, before the effect.
4. Fail closed on wrong account, redirect, changed target/context, expired or revoked permission, ambiguous selector, login challenge, or other unexpected UI state.
5. Never retry or submit again after an ambiguous outcome; require manual reconciliation before any separately reviewed next step.
6. Stop with zero remote effect whenever a required check fails.

None of those checks, journals, confirmations, or effects is implemented by BJ-06. This document cannot be treated as an executable one-action approval. A human decision on a suggestion does not itself establish platform rights.

## Privacy, misuse, and authorization boundaries

Engagement can affect another person and can make a false claim on the operator’s behalf. Therefore, the design excludes unauthorized or unsolicited outreach, bulk template distribution, random or probability-driven engagement, follower-growth manipulation, persona simulation, stealth, cookie or private-profile access, private API/CDP/XHR use, CAPTCHA bypass, and transfer of post or applicant information to third-party AI, CRM, or messaging services. Do not collect, disclose, or retain real user/customer identities, contact details, credentials, or private content for these examples or public CI evidence.

A publicly visible post does not by itself grant permission to collect, copy, reuse, or contact its author. This document does not verify Meta terms or platform rights and requires no live test.

Keep these gates separate:

- **#103 and #64:** authority for reading or collecting live Threads content remains NO_GO until separately verified.
- **#67:** permission and implementation for any UI effect remain separate and are NO_GO.
- **#2:** source ownership, provenance, and redistribution/license decision remains unresolved.
- **#18 and #46:** fresh-install and customer/local UAT remain separate acceptance gates.

Internal synthetic fixtures may support offline design review only. They do not establish live-platform permission, public visibility, customer readiness, or release/distribution eligibility.

## Decision and stop conditions

The outcome is **DESIGN_READY_FOR_MANUAL_HUMAN_REVIEW_ONLY**, not **BROWSER_ENGAGEMENT_READY**. It accepts only a manual-review design. It does not claim that a feature, runtime control, browser action, real proposal, or remote effect exists.

Stop and return NO_PROPOSAL or STOPPED if provenance is unknown, context is insufficient or stale, the target/account binding changes, the target redirects, the draft/template changes or expires, the reviewer cannot inspect the exact draft, a duplicate or ambiguous outcome exists, authorization is missing or revoked, or an unexpected challenge/UI state occurs. Do not retry an ambiguous effect. Resolve only through a separately authorized human process; do not weaken a check to make a proposal proceed.

No implementation, live browser access, network request, account access, or engagement test is authorized by this document.
