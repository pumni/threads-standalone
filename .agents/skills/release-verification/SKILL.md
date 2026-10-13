---
name: release-verification
description: Use for final verification of a pull request, package, or release handoff, including selecting evidence for its exact commit. Skip routine edits that are not at a release or final-review gate.
---

# Release verification

Verify evidence against the exact change under review. A prior checkpoint, green default branch, or draft plan is not evidence for a newer head.

1. Confirm the current handoff, base and head SHAs, worktree status, open ownership conflicts, and exact changed-file manifest. Keep the pull request within its authorized scope.
2. Read [AGENTS.md](../../../AGENTS.md) and [CONTEXT_MAP.md](../../../docs/CONTEXT_MAP.md) to determine the checks that apply. Label each result PASS, FAIL, or NOT RUN and state the evidence and commit it covers.
3. For a docs-only change, inspect content and repository-relative links, then run git diff --check. Per current repository rules, local product tests, lint, type checks, builds, and wheel smoke are NOT RUN for docs-only work. Review applicable hosted checks at the exact PR head without expanding the task into unrelated CI repair.
4. For an authorized code or package release, follow the current locked quality gates: uv sync --locked, Ruff lint and format, strict Pyright, synthetic pytest, uv build, and installed-wheel smoke outside the checkout with PYTHONPATH unset. Review hosted Windows and Ubuntu workflows and full-history Gitleaks at that same SHA.
5. Check that release claims match the evidence. Keep synthetic verification separate from live UAT, API permission, customer acceptance, and third-party visibility. Preserve the unresolved provenance and license status in [PROVENANCE_DECISION.md](../../../docs/PROVENANCE_DECISION.md); consult [BROWSER_MVP_RELEASE_READINESS.md](../../../docs/BROWSER_MVP_RELEASE_READINESS.md) only when relevant.
6. Prepare a narrow Draft PR with the exact base/head SHA, changed-file manifest, checks, and blockers for human review. Do not merge, publish, or claim a release without the separate applicable authorization.

## Read next

- [Repository rules](../../../AGENTS.md)
- [Context map](../../../docs/CONTEXT_MAP.md)
- [CI workflow](../../../.github/workflows/ci.yml)
- [Provenance and license decision](../../../docs/PROVENANCE_DECISION.md)
- [Browser release readiness](../../../docs/BROWSER_MVP_RELEASE_READINESS.md)
- [Clean package test](../../../tests/test_clean_package.py)