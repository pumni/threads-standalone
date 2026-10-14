---
name: release-verification
description: Use for final verification of a pull request, package, or release handoff, including selecting evidence for its exact commit. Skip routine edits that are not at a release or final-review gate.
---

# Release verification

Verify evidence against the exact change under review. A prior checkpoint, green default branch, or draft plan is not evidence for a newer head.

1. Confirm the current handoff, base and head SHAs, worktree status, open ownership conflicts, and exact changed-file manifest. Keep the pull request within its authorized scope.
2. Read [AGENTS.md](../../../AGENTS.md), [CONTEXT_MAP.md](../../../docs/CONTEXT_MAP.md), and the lane manifest in [ci.py](../../../.github/scripts/ci.py). Label every result PASS, FAIL, or NOT RUN and state the evidence and commit it covers.
3. For docs-only changes, inspect content and repository-relative links, then run `git diff --check`. Local product tests, lint, type checks, builds, and wheel smoke are NOT RUN (docs-only). Review Gitleaks and documentation checks at the exact PR head.
4. For development, run the changed behavior's focused synthetic tests and relevant Ruff/type checks. Do not run the full Browser suite for API-only work or API-specific tests for Browser-only work by default.
5. For PR verification, use the `CI` workflow's lane selection and stable `required CI gate`. Every PR requires full-history Gitleaks; code lanes also require their selected Linux/Windows jobs and the targeted installed-wheel smoke outside the checkout with `PYTHONPATH` unset and zero skips. `DOCS_ONLY`, `API_ONLY`, `BROWSER_ONLY`, `SHARED`, and `FULL` select different checks; the helper routes unknown files and CI/dependency/test-infrastructure changes to `FULL`. Confirm the chosen lane and passing gate on the exact PR head.
6. For full regression or release, review `uv sync --locked`, Ruff lint/format, strict Pyright, full synthetic tests on Ubuntu and Windows, Chromium-backed Browser coverage, `uv build`, targeted installed-wheel smoke, and full-history Gitleaks at the same SHA. Full tests report pytest durations to show the slowest cases.
7. Check that release claims match the evidence. Keep synthetic verification separate from live UAT, API permission, customer acceptance, and third-party visibility. Preserve the unresolved provenance and license status in [PROVENANCE_DECISION.md](../../../docs/PROVENANCE_DECISION.md); consult [BROWSER_MVP_RELEASE_READINESS.md](../../../docs/BROWSER_MVP_RELEASE_READINESS.md) only when relevant.
8. Prepare a narrow Draft PR with the exact base/head SHA, changed-file manifest, selected lane, checks, and blockers for human review. Do not merge, publish, or claim a release without the separate applicable authorization.

## Read next

- [Repository rules](../../../AGENTS.md)
- [Context map](../../../docs/CONTEXT_MAP.md)
- [CI workflow](../../../.github/workflows/ci.yml)
- [Provenance and license decision](../../../docs/PROVENANCE_DECISION.md)
- [Browser release readiness](../../../docs/BROWSER_MVP_RELEASE_READINESS.md)
- [Clean package test](../../../tests/test_clean_package.py)
