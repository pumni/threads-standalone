from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CI_HELPER_PATH = _ROOT / ".github" / "scripts" / "ci.py"
_CI_SPEC = importlib.util.spec_from_file_location("threads_standalone_ci", _CI_HELPER_PATH)
assert _CI_SPEC is not None and _CI_SPEC.loader is not None
ci: ModuleType = importlib.util.module_from_spec(_CI_SPEC)
sys.modules[_CI_SPEC.name] = ci
_CI_SPEC.loader.exec_module(ci)


def test_name_status_parser_keeps_renamed_and_deleted_paths() -> None:
    changed = ci.parse_name_status(
        b"M\0src/threads_platform/api/client.py\0"
        b"R100\0tests/test_api_runtime.py\0tests/test_browser_runtime.py\0"
        b"D\0tests/test_api_contract.py\0"
    )

    assert [(item.status, item.path) for item in changed] == [
        ("M", "src/threads_platform/api/client.py"),
        ("R100", "tests/test_api_runtime.py"),
        ("R100", "tests/test_browser_runtime.py"),
        ("D", "tests/test_api_contract.py"),
    ]
    deleted = ci.classify_paths([item.path for item in changed if item.status == "D"])
    rename = ci.classify_paths([item.path for item in changed if item.status.startswith("R")])

    assert deleted.lane == "API_ONLY"
    assert rename.lane == "FULL"
    assert ci.required_jobs(rename.lane) == ("linux", "windows_full")


def test_docs_and_single_lane_changes_keep_their_lane() -> None:
    docs = ci.classify_paths(
        ["README.md", "docs/guide.md", ".agents/skills/release-verification/SKILL.md"]
    )
    api = ci.classify_paths(["src/threads_platform/api/client.py", "README.md"])
    browser = ci.classify_paths(
        ["src/threads_platform/browser/threads_job_ui_reader.py", "tests/test_browser_jobs_e2e.py"]
    )

    assert (docs.lane, docs.has_docs) == ("DOCS_ONLY", True)
    assert (api.lane, api.has_docs) == ("API_ONLY", True)
    assert browser.lane == "BROWSER_ONLY"


def test_mixed_source_lanes_choose_full_regression() -> None:
    cases = (
        ["src/threads_platform/api/client.py", "src/threads_platform/browser/semantics.py"],
        ["src/threads_platform/api/client.py", "src/threads_platform/standalone/accounts.py"],
        ["src/threads_platform/browser/semantics.py", "src/threads_platform/standalone/runtime.py"],
        [
            "src/threads_platform/api/client.py",
            "src/threads_platform/browser/semantics.py",
            "src/threads_platform/standalone/accounts.py",
        ],
    )

    for paths in cases:
        result = ci.classify_paths(paths)
        assert result.lane == "FULL"
        assert ci.required_jobs(result.lane) == ("linux", "windows_full")


def test_markdown_does_not_create_a_mixed_source_lane() -> None:
    api = ci.classify_paths(["src/threads_platform/api/client.py", "README.md"])
    browser = ci.classify_paths(["src/threads_platform/browser/semantics.py", "docs/browser.md"])

    assert (api.lane, api.has_docs) == ("API_ONLY", True)
    assert (browser.lane, browser.has_docs) == ("BROWSER_ONLY", True)


def test_unknown_and_known_paths_choose_full_regression() -> None:
    shared = ci.classify_paths(["src/threads_platform/standalone/accounts.py"])
    unknown = ci.classify_paths(["src/threads_platform/standalone/new_module.py"])
    mixed = ci.classify_paths(["src/threads_platform/api/client.py", "tools/new-ci-helper.py"])
    config = ci.classify_paths(["pyproject.toml"])

    assert shared.lane == "SHARED"
    assert unknown.lane == "FULL"
    assert mixed.lane == "FULL"
    assert config.lane == "FULL"


def test_test_routing_uses_exact_repo_relative_paths() -> None:
    root_module = ci.classify_paths(["tests/test_api_runtime.py"])
    nested_duplicate = ci.classify_paths(["tests/regression/test_api_runtime.py"])
    nested_unknown = ci.classify_paths(["tests/regression/test_new_runtime.py"])

    assert root_module.lane == "API_ONLY"
    assert nested_duplicate.lane == "FULL"
    assert nested_unknown.lane == "FULL"


def test_push_and_manual_regression_routes_are_always_full(monkeypatch) -> None:
    monkeypatch.setenv("CI_EVENT_NAME", "workflow_dispatch")
    assert ci.route_changes().lane == "FULL"

    monkeypatch.setenv("CI_EVENT_NAME", "push")
    monkeypatch.setenv("CI_BEFORE_SHA", "0" * 40)
    monkeypatch.setenv("CI_AFTER_SHA", "a" * 40)
    assert ci.route_changes().lane == "FULL"


def test_lane_test_lists_partition_every_test_module_recursively() -> None:
    configured = set(ci._TEST_LANES)
    discovered = {
        path.relative_to(_ROOT).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in (_ROOT / "tests").rglob(pattern)
        if path.is_file()
    }

    assert len(configured) == (
        len(ci.API_TESTS) + len(ci.BROWSER_TESTS) + len(ci.SHARED_TESTS) + len(ci.FULL_ONLY_TESTS)
    )
    assert configured == discovered
    assert ci._test_manifest_errors(_ROOT) == []


def test_recursive_test_manifest_catches_new_nested_and_duplicate_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_root = tmp_path / "tests"
    nested = test_root / "regression"
    nested.mkdir(parents=True)
    (test_root / "test_api_runtime.py").touch()
    (nested / "test_api_runtime.py").touch()
    (nested / "new_runtime_test.py").touch()
    monkeypatch.setattr(ci, "_TEST_LANES", {"tests/test_api_runtime.py": "API"})

    errors = ci._test_manifest_errors(tmp_path)

    assert errors == [
        (
            "unclassified test modules were discovered: "
            "tests/regression/new_runtime_test.py, tests/regression/test_api_runtime.py"
        )
    ]
    assert ci.classify_paths(["tests/regression/test_api_runtime.py"]).lane == "FULL"


def test_deleted_configured_test_fails_manifest_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(ci, "_TEST_LANES", {"tests/test_api_runtime.py": "API"})

    assert ci.classify_paths(["tests/test_api_runtime.py"]).lane == "API_ONLY"
    assert ci._test_manifest_errors(tmp_path) == [
        "configured test modules are missing: tests/test_api_runtime.py"
    ]


def test_windows_browser_acceptance_does_not_select_api_runtime_suites() -> None:
    shared = {path.name for path in ci._test_files("windows-smoke", _ROOT / "tests")}
    browser = {path.name for path in ci._test_files("windows-browser", _ROOT / "tests")}

    assert {"test_api_runtime.py", "test_mutation_runtime.py"} <= shared
    assert {"test_api_runtime.py", "test_mutation_runtime.py"}.isdisjoint(browser)
    assert {
        "test_browser_engine.py",
        "test_browser_mvp_integration.py",
        "test_browser_runtime.py",
        "test_cross_platform_state.py",
        "test_state_compatibility.py",
    } <= shared


def test_required_gate_rejects_skips_and_missing_docs_validation() -> None:
    api_results = {
        "classify": "success",
        "linux": "success",
        "windows_smoke": "success",
        "windows_browser": "skipped",
        "windows_full": "skipped",
    }
    assert ci.gate_errors("API_ONLY", False, "", api_results) == []
    assert (
        ci.gate_errors(
            "DOCS_ONLY",
            True,
            "success",
            {
                "classify": "success",
                "linux": "skipped",
                "windows_smoke": "skipped",
                "windows_browser": "skipped",
                "windows_full": "skipped",
            },
        )
        == []
    )
    assert ci.gate_errors("DOCS_ONLY", True, "", {"classify": "success"}) == [
        "documentation check result was missing"
    ]
    assert ci.gate_errors(
        "FULL",
        False,
        "",
        {"classify": "success", "linux": "success", "windows_full": "cancelled"},
    ) == ["required windows_full job result was cancelled"]
    assert ci.gate_errors("BROWSER_ONLY", False, "", {"classify": "success"}) == [
        "required linux job result was missing",
        "required windows_browser job result was missing",
    ]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "missing"])
@pytest.mark.parametrize(
    ("lane", "job"),
    [
        ("API_ONLY", "linux"),
        ("API_ONLY", "windows_smoke"),
        ("BROWSER_ONLY", "linux"),
        ("BROWSER_ONLY", "windows_browser"),
        ("SHARED", "linux"),
        ("SHARED", "windows_smoke"),
        ("FULL", "linux"),
        ("FULL", "windows_full"),
    ],
)
def test_required_gate_rejects_each_unavailable_selected_job(
    lane: str, job: str, result: str
) -> None:
    results = {"classify": "success"}
    results.update({required: "success" for required in ci.required_jobs(lane) or ()})
    if result == "missing":
        results.pop(job)
    else:
        results[job] = result

    errors = ci.gate_errors(lane, False, "", results)

    assert errors == [f"required {job} job result was {result}"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "missing"])
def test_required_gate_rejects_unavailable_classification(result: str) -> None:
    results = {"classify": result} if result != "missing" else {}

    assert ci.gate_errors("FULL", False, "", results)[0].startswith("classify/security result was")


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "missing"])
def test_required_gate_rejects_unavailable_docs_validation(result: str) -> None:
    results = {"classify": "success"}

    errors = ci.gate_errors("API_ONLY", True, result, results)

    assert (
        errors[0]
        == f"documentation check result was {result if result != 'missing' else 'missing'}"
    )


def test_required_gate_rejects_unknown_lane_and_invalid_docs_only_classification() -> None:
    assert ci.gate_errors("API_ONLY", True, "failure", {"classify": "success"}) == [
        "documentation check result was failure",
        "required linux job result was missing",
        "required windows_smoke job result was missing",
    ]
    assert ci.gate_errors("", False, "", {}) == ["unknown or missing classification: ''"]
    assert ci.gate_errors("DOCS_ONLY", False, "", {"classify": "success"}) == [
        "DOCS_ONLY classification had no changed Markdown file"
    ]


def test_concurrency_is_scoped_to_one_pr_and_only_pr_runs_are_cancellable() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "group: ci-${{ github.event.pull_request.number || github.run_id }}" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow


def test_windows_shared_job_starts_api_and_browser_cli_without_chromium_setup() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    windows_smoke = workflow.split("  windows-smoke:", maxsplit=1)[1].split(
        "  windows-browser:", maxsplit=1
    )[0]

    assert "uv run --locked threads-local --help" in windows_smoke
    assert "uv run --locked threads-local api --help" in windows_smoke
    assert "uv run --locked threads-local browser --help" in windows_smoke
    assert "playwright install" not in windows_smoke


def test_junit_counts_aggregate_pytest_suites_container() -> None:
    report = ET.fromstring(
        '<testsuites><testsuite tests="7" skipped="1" failures="0" errors="0" />'
        '<testsuite tests="9" skipped="0" failures="0" errors="0" /></testsuites>'
    )

    assert ci.junit_counts(report) == {"tests": 16, "skipped": 1, "failures": 0, "errors": 0}


def test_markdown_link_checker_checks_local_targets_and_ignores_external_links(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        "[good](docs/guide.md) [external](https://example.com) [broken](docs/missing.md)\n",
        encoding="utf-8",
    )

    assert ci.check_markdown_links(tmp_path, Path("README.md")) == ["README.md -> docs/missing.md"]
