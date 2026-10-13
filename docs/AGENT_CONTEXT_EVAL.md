# Agent context evaluation — AC-04

Issue [#147](https://github.com/pumni/threads-standalone/issues/147), under epic #143. This is a small qualitative routing check, not a benchmark.

## Method

- Base: origin/main at 66ab40fde843da48a0a04591750896982767859a.
- Local Codex CLI: codex-cli 0.162.1.
- Each scenario ran in a fresh Codex process with JSONL output, ephemeral session storage, and read-only sandbox. The prompt text below is the sanitized text sent to the CLI.
- The retained trace output included command executions and agent messages, but did not preserve a dedicated activation event. Positive activation is OBSERVED only where the trace directly showed a command reading that skill’s SKILL.md. A skill merely present in .agents/skills, available by metadata, or named in an agent message does not count.
- For negative cases, no direct skill read in the retained trace does not prove non-activation; no explicit non-activation event was captured, so those results remain UNVERIFIED.
- “Direct read” below means a Get-Content command appeared in the JSONL trace. “Search/reference” means a command searched or named a path, not that the full document was read. A path mentioned only in a final answer is marked UNVERIFIED as a read.
- This matches the [OpenAI Skills documentation](https://learn.chatgpt.com/docs/build-skills): Codex can select a skill implicitly from its description and loads full instructions when selected. The [OpenAI skill-evaluation guide](https://developers.openai.com/blog/eval-skills) recommends evaluating captured run traces.
- No files were changed during the scenarios. No tests, builds, lint/type checks, CI, browser, Threads API, credentials, or customer/operator data were used.

## Results

| Scenario | Skill activation evidence | Routing and safety outcome |
| --- | --- | --- |
| 1. Trivial README typo | UNVERIFIED: no skill-file read was observed; the final response said none applied. | FAIL: read the full context map and searched source, pyproject.toml, README.md, and the map for “skills.” It did not read the requested README passage and made an unsupported claim that product sources establish whether Codex loads skills. |
| 2. NurtureStore v1 schema/recovery | OBSERVED: [state-compatibility-review/SKILL.md](../.agents/skills/state-compatibility-review/SKILL.md) was read in the bounded run. | UNVERIFIED overall: the bounded answer correctly stated no silent v1 migration, require a reviewed versioned contract and explicit ownership, and block AMBIGUOUS retries. The initial run was interrupted before its final answer after broad searches across docs, src, and tests. The bounded response named [Nurture parity](NURTURE_PARITY.md), [state compatibility rehearsal](STATE_COMPATIBILITY_REHEARSAL.md), and [mutation parity](MUTATION_PARITY.md), but direct read evidence for those paths was not retained. |
| 3. API reply dispatch | OBSERVED: [threads-api-effect-review/SKILL.md](../.agents/skills/threads-api-effect-review/SKILL.md) was read. [state-compatibility-review/SKILL.md](../.agents/skills/state-compatibility-review/SKILL.md) was announced as applicable, but no direct read was observed; its activation is UNVERIFIED. | PASS for the API route: the answer required endpoint-specific permission, exact account/actor/target/final-text review, the existing journal, and no automatic retry after AMBIGUOUS. [CONTEXT_MAP.md](CONTEXT_MAP.md) was directly read; mutation, recruitment, and API parity paths appeared in search/reference commands. |
| 4. Live browser collection | UNVERIFIED: no skill-file read was observed. The answer said no API-effect skill applied, but this does not prove non-activation. | PASS for the safety route: it required separate browser/platform permission and distinguished synthetic checks from live permission. [CONTEXT_MAP.md](CONTEXT_MAP.md) was directly read; [BROWSER_PARITY.md](BROWSER_PARITY.md) and [BROWSER_MVP_RELEASE_READINESS.md](BROWSER_MVP_RELEASE_READINESS.md) appeared in search/reference commands. No browser or network was used. |
| 5. Final package/PR acceptance | OBSERVED: [release-verification/SKILL.md](../.agents/skills/release-verification/SKILL.md) was read. | PASS for the core route: the answer distinguished exact-head evidence from an older checkpoint and docs-only NOT RUN steps from code gates. [CONTEXT_MAP.md](CONTEXT_MAP.md), the [CI workflow](../.github/workflows/ci.yml), [PROVENANCE_DECISION.md](PROVENANCE_DECISION.md), and [BROWSER_MVP_RELEASE_READINESS.md](BROWSER_MVP_RELEASE_READINESS.md) were directly read. [AGENTS.md](../AGENTS.md) and [test_clean_package.py](../tests/test_clean_package.py) appeared in the response, but direct read evidence is UNVERIFIED. Browser readiness was extra context for this generic prompt; its relevance is unclear. One read/search command exited 1, but its command detail was not retained; that result is UNVERIFIED. No verification gate ran. |

### Exact prompts

#### 1. Trivial README typo

> Read-only context-routing check. Suppose a user asks: Fix a misspelling in README.md. Do not edit files or run tests. Based on the repository instructions and context map, tell me the minimum routing context for that task and why. Do not inspect unrelated API, browser, release, or state contracts. Name a skill only if the CLI actually loads its instructions.

#### 2. NurtureStore v1 schema and recovery

Initial prompt:

> Read-only hypothetical review only: suppose a developer proposes changing NurtureStore v1 persisted schema and restart recovery. Do not edit schema, run tests, inspect data roots, or perform migrations. Identify the minimum repository context and safeguards needed for compatibility, versioning, and AMBIGUOUS no-retry behavior. Name a skill only if the CLI actually loads its instructions.

The first run directly read the state skill, then expanded into broad source, test, and document searches; it was interrupted before a final answer. A bounded fresh run used this prompt:

> Read-only context-routing check, not a code review: hypothetically, a proposed NurtureStore v1 schema/recovery change. Do not edit files, run tests, search source/test trees, or access any data roots. Use the context map to select only directly relevant documentation and load an applicable skill if you choose one. Do not search repository-wide. In at most 100 words, state the selected context, skill if loaded, the version/migration guard, and the AMBIGUOUS retry guard.

#### 3. Official Threads API reply dispatch

> Read-only context-routing exercise. Hypothetical request: add a Threads API reply-dispatch effect. Do not call APIs, use credentials, edit files, access accounts, or run tests. Use the context map to identify the minimum applicable skill and documentation. State the endpoint permission, exact account/actor/target/final-text review, operation-journal requirement, and AMBIGUOUS rule. Do not inspect source or tests; answer in at most 100 words.

The same prompt ran twice in fresh sessions; the second trace captured the direct skill-file read.

#### 4. Live browser collection

> Read-only routing exercise. Hypothetical user request: use Playwright for live browser collection of public Threads posts. Do not start a browser, access network/accounts/credentials, edit files, or run tests. Identify the applicable browser contract and the separate authorization required; distinguish synthetic evidence from live permission. State whether any API-effect skill should activate. Use only relevant context/docs, not source or tests. Answer in at most 100 words.

#### 5. Final package/PR acceptance

> Read-only routing exercise. Hypothetical final PR/package acceptance request. Do not run tests, lint, type checks, builds, wheel smoke, or CI; do not edit files. Use the context map to select the applicable release skill and minimum documents. Distinguish exact-head evidence from older checkpoints, and docs-only NOT RUN steps from code-PR gates. Answer in at most 100 words.

## Evidence-backed recommendations

- KEEP the state, API-effect, and release skills: each was directly loaded in its matching positive scenario and the corresponding answer included the intended safeguards.
- ADJUST context discipline for simple documentation tasks: the typo scenario triggered a broad search unrelated to the requested passage. Prefer the affected file and applicable repository instructions; do not search product code for skill-loading behavior.
- ADJUST final-review routing to open browser readiness material only when the change or release claim makes it relevant.
- Do not claim improved focus, safety, quality, token use, or elapsed time. There is no measured baseline. The two negative-scenario non-activation results remain UNVERIFIED because the trace showed no direct skill load but did not provide a dedicated non-activation event. Remove no skill on this evidence.