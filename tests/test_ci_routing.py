from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

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
    assert rename.lane == "SHARED"


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


def test_shared_multiple_lanes_and_unknown_paths_choose_safe_regressions() -> None:
    shared = ci.classify_paths(["src/threads_platform/standalone/accounts.py"])
    multiple = ci.classify_paths(
        ["src/threads_platform/api/client.py", "src/threads_platform/browser/semantics.py"]
    )
    unknown = ci.classify_paths(["src/threads_platform/standalone/new_module.py"])
    config = ci.classify_paths(["pyproject.toml"])

    assert shared.lane == "SHARED"
    assert multiple.lane == "SHARED"
    assert unknown.lane == "FULL"
    assert config.lane == "FULL"


def test_push_and_manual_regression_routes_are_always_full(monkeypatch) -> None:
    monkeypatch.setenv("CI_EVENT_NAME", "workflow_dispatch")
    assert ci.route_changes().lane == "FULL"

    monkeypatch.setenv("CI_EVENT_NAME", "push")
    monkeypatch.setenv("CI_BEFORE_SHA", "0" * 40)
    monkeypatch.setenv("CI_AFTER_SHA", "a" * 40)
    assert ci.route_changes().lane == "FULL"


def test_lane_test_lists_partition_every_test_module() -> None:
    configured = (
        set(ci.API_TESTS) | set(ci.BROWSER_TESTS) | set(ci.SHARED_TESTS) | set(ci.FULL_ONLY_TESTS)
    )
    discovered = {path.name for path in (_ROOT / "tests").glob("test_*.py")}

    assert configured == discovered


def test_windows_browser_acceptance_does_not_select_api_runtime_suites() -> None:
    shared = {path.name for path in ci._test_files("windows-smoke", _ROOT / "tests")}
    browser = {path.name for path in ci._test_files("windows-browser", _ROOT / "tests")}

    assert {"test_api_runtime.py", "test_mutation_runtime.py"} <= shared
    assert {"test_api_runtime.py", "test_mutation_runtime.py"}.isdisjoint(browser)


def test_required_gate_rejects_skips_and_missing_docs_validation() -> None:
    api_results = {
        "classify": "success",
        "linux": "success",
        "windows_smoke": "success",
    }
    assert ci.gate_errors("API_ONLY", False, "", api_results) == []
    assert ci.gate_errors("DOCS_ONLY", True, "success", {"classify": "success"}) == []
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
