from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

# These lists are the lane contract. New or unclassified test/source paths fall back to FULL.
API_TESTS = (
    "test_api_auto_content.py",
    "test_api_auto_plan.py",
    "test_api_auto_review_context.py",
    "test_api_auto_review_packet.py",
    "test_api_auto_review_tty.py",
    "test_api_contract.py",
    "test_api_discovery.py",
    "test_api_runtime.py",
    "test_customer_recruitment_demo.py",
    "test_mutation_api.py",
    "test_mutation_cli.py",
    "test_mutation_runtime.py",
    "test_nurture.py",
    "test_nurture_apply.py",
    "test_nurture_cli.py",
    "test_nurture_content.py",
    "test_nurture_conversations.py",
    "test_nurture_discovery.py",
    "test_nurture_durability.py",
    "test_nurture_insights.py",
    "test_nurture_own_content.py",
    "test_nurture_runner.py",
    "test_nurture_runner_inbound.py",
    "test_opportunity_queue.py",
    "test_opportunity_queue_v2.py",
    "test_recurrence_cli.py",
    "test_recurrences.py",
    "test_recruitment_atomic_handoff.py",
    "test_recruitment_context_approval.py",
    "test_recruitment_content_calendar.py",
    "test_recruitment_content_cli.py",
    "test_recruitment_content_planner.py",
    "test_recruitment_crm_export.py",
    "test_recruitment_crm_review.py",
    "test_recruitment_crm_review_tty.py",
    "test_recruitment_e2e.py",
    "test_recruitment_followup_projection.py",
    "test_recruitment_journey.py",
    "test_recruitment_qualification.py",
    "test_recruitment_reply_assistant.py",
    "test_recruitment_reply_proposal.py",
    "test_recruitment_session.py",
    "test_recruitment_shortlist.py",
    "test_recruitment_watch.py",
    "test_recruitment_watch_stability.py",
    "test_s12b1_crm_export_security_contract.py",
    "test_s13a_offline_acceptance.py",
    "test_workflow_cli.py",
    "test_workflows.py",
)

BROWSER_TESTS = (
    "test_browser_contract.py",
    "test_browser_engine.py",
    "test_browser_job_observation.py",
    "test_browser_job_watch.py",
    "test_browser_jobs_e2e.py",
    "test_browser_mvp_integration.py",
    "test_browser_nurture_e2e.py",
    "test_browser_opportunity_bridge.py",
    "test_browser_read_adapters.py",
    "test_browser_reader_s11a.py",
    "test_browser_runtime.py",
    "test_browser_security.py",
    "test_browser_semantics.py",
    "test_browser_ui_diagnostic.py",
    "test_browser_ui_reader_e2e.py",
    "test_recruitment_browser_jobs_cli.py",
    "test_recruitment_job_store.py",
    "test_recruitment_job_vacancy.py",
    "test_ui_card_snapshot.py",
)

SHARED_TESTS = (
    "test_accounts.py",
    "test_architecture_boundary.py",
    "test_cli_smoke.py",
    "test_cross_platform_state.py",
    "test_credentials.py",
    "test_manual_opportunities.py",
    "test_manual_workflow.py",
    "test_public_security.py",
    "test_s9b2a_handoff_lock_contract.py",
    "test_state_compatibility.py",
)

# These integration/package/infrastructure tests always run in FULL regression. The package
# audit also runs directly against the installed wheel in every code PR's targeted wheel smoke.
FULL_ONLY_TESTS = (
    "test_ci_routing.py",
    "test_clean_package.py",
    "test_full_synthetic_e2e.py",
)

WINDOWS_SHARED_SMOKE_TESTS = (
    "test_accounts.py",
    "test_architecture_boundary.py",
    "test_browser_engine.py",
    "test_browser_mvp_integration.py",
    "test_browser_runtime.py",
    "test_browser_security.py",
    "test_cli_smoke.py",
    "test_credentials.py",
    "test_cross_platform_state.py",
    "test_public_security.py",
    "test_state_compatibility.py",
)
WINDOWS_API_SMOKE_TESTS = ("test_api_runtime.py", "test_mutation_runtime.py")

_API_SOURCE_FILES = frozenset(
    f"src/threads_platform/standalone/{name}"
    for name in (
        "api.py",
        "api_auto_content.py",
        "api_auto_plan.py",
        "api_auto_review_context.py",
        "api_auto_review_tty.py",
        "mutations.py",
        "nurture.py",
        "nurture_content.py",
        "nurture_conversations.py",
        "nurture_discovery.py",
        "nurture_draft.py",
        "nurture_insights.py",
        "nurture_policy.py",
        "nurture_quote_draft.py",
        "nurture_runner.py",
        "nurture_store.py",
        "opportunity_queue.py",
        "opportunity_queue_v2.py",
        "recurrences.py",
        "recruitment_context_approval.py",
        "recruitment_content_calendar.py",
        "recruitment_content_cli.py",
        "recruitment_content_planner.py",
        "recruitment_crm_export.py",
        "recruitment_crm_review.py",
        "recruitment_crm_review_tty.py",
        "recruitment_followup_projection.py",
        "recruitment_qualification.py",
        "recruitment_reply_assistant.py",
        "recruitment_reply_proposal.py",
        "recruitment_session.py",
        "recruitment_watch.py",
        "workflows.py",
    )
)
_SHARED_SOURCE_FILES = frozenset(
    {
        "src/threads_platform/__init__.py",
        "src/threads_platform/config.py",
        "src/threads_platform/standalone/__init__.py",
        "src/threads_platform/standalone/__main__.py",
        "src/threads_platform/standalone/accounts.py",
        "src/threads_platform/standalone/app.py",
        "src/threads_platform/standalone/manual_opportunities.py",
        "src/threads_platform/standalone/manual_outcomes.py",
        "src/threads_platform/standalone/manual_workflow.py",
        "src/threads_platform/standalone/runtime.py",
    }
)
_BROWSER_SOURCE_FILES = frozenset(
    f"src/threads_platform/standalone/{name}"
    for name in (
        "browser_job_watch.py",
        "browser_nurture.py",
        "browser_nurture_state.py",
        "browser_opportunity_bridge.py",
        "browser_ui_diagnostic.py",
        "recruitment_browser_jobs_browser_source.py",
        "recruitment_browser_jobs_cli.py",
        "recruitment_job_store.py",
        "recruitment_job_vacancy.py",
    )
)
_TEST_LANES = {
    **{f"tests/{path}": "API" for path in API_TESTS},
    **{f"tests/{path}": "BROWSER" for path in BROWSER_TESTS},
    **{f"tests/{path}": "SHARED" for path in SHARED_TESTS},
    **{f"tests/{path}": "FULL" for path in FULL_ONLY_TESTS},
}
_REQUIRED_JOBS = {
    "DOCS_ONLY": (),
    "API_ONLY": ("linux", "windows_smoke"),
    "BROWSER_ONLY": ("linux", "windows_browser"),
    "SHARED": ("linux", "windows_smoke"),
    "FULL": ("linux", "windows_full"),
}
_MARKDOWN_LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^\s)]+))")
_MARKDOWN_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str


@dataclass(frozen=True)
class Classification:
    lane: str
    has_docs: bool
    paths: tuple[str, ...]


def parse_name_status(output: bytes) -> list[ChangedPath]:
    fields = output.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    changes: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        for path in fields[index : index + path_count]:
            changes.append(ChangedPath(status=status, path=path.replace("\\", "/")))
        index += path_count
    return changes


def _is_markdown(path: str) -> bool:
    return Path(path).suffix.casefold() in {".md", ".markdown"}


def _path_lane(path: str) -> str | None:
    normalized = path.strip("/")
    if normalized in {"pyproject.toml", "uv.lock", "pytest.ini", "tox.ini"}:
        return "FULL"
    if normalized.startswith((".github/", ".agents/")):
        return "FULL"
    if normalized.startswith("tests/"):
        return _TEST_LANES.get(normalized, "FULL")
    if normalized.startswith("src/threads_platform/api/"):
        return "API"
    if normalized.startswith("src/threads_platform/browser/"):
        return "BROWSER"
    if normalized in _API_SOURCE_FILES:
        return "API"
    if normalized in _BROWSER_SOURCE_FILES:
        return "BROWSER"
    if normalized in _SHARED_SOURCE_FILES or normalized.startswith(
        ("src/threads_platform/credentials/", "src/threads_platform/locks/")
    ):
        return "SHARED"
    if normalized.startswith("src/threads_platform/"):
        return "FULL"
    return None


def classify_paths(paths: list[str] | tuple[str, ...]) -> Classification:
    normalized_paths = tuple(sorted({path.replace("\\", "/").strip("/") for path in paths}))
    has_docs = any(_is_markdown(path) for path in normalized_paths)
    if not normalized_paths:
        return Classification("FULL", has_docs=False, paths=())

    code_paths = [path for path in normalized_paths if not _is_markdown(path)]
    if not code_paths:
        return Classification("DOCS_ONLY", has_docs=True, paths=normalized_paths)

    lanes: set[str] = set()
    for path in code_paths:
        lane = _path_lane(path)
        if lane in {None, "FULL"}:
            return Classification("FULL", has_docs=has_docs, paths=normalized_paths)
        lanes.add(lane)

    if len(lanes) > 1:
        lane_name = "FULL"
    elif lanes == {"SHARED"}:
        lane_name = "SHARED"
    elif lanes == {"API"}:
        lane_name = "API_ONLY"
    elif lanes == {"BROWSER"}:
        lane_name = "BROWSER_ONLY"
    else:
        lane_name = "FULL"
    return Classification(lane_name, has_docs=has_docs, paths=normalized_paths)


def _event_diff_args() -> list[str] | None:
    event = os.environ.get("CI_EVENT_NAME", os.environ.get("GITHUB_EVENT_NAME", ""))
    if event == "pull_request":
        base = os.environ.get("CI_BASE_SHA", "")
        head = os.environ.get("CI_HEAD_SHA", "")
        return [f"{base}...{head}"] if base and head else None
    if event == "push":
        before = os.environ.get("CI_BEFORE_SHA", "")
        after = os.environ.get("CI_AFTER_SHA", "")
        if before and after and set(before) != {"0"}:
            return [before, after]
    return None


def _changed_paths(diff_args: list[str] | None) -> list[ChangedPath]:
    if diff_args is None:
        return []
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", "-M", *diff_args],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        print("Could not calculate the changed-file diff; using FULL regression.", file=sys.stderr)
        return []
    return parse_name_status(result.stdout)


def _write_github_output(values: dict[str, str]) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("a", encoding="utf-8", newline="\n") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in values.items())


def route_changes() -> Classification:
    event = os.environ.get("CI_EVENT_NAME", os.environ.get("GITHUB_EVENT_NAME", ""))
    diff_args = _event_diff_args()
    changes = _changed_paths(diff_args)
    changed_names = [change.path for change in changes]
    result = classify_paths(changed_names)
    if event in {"push", "workflow_dispatch"}:
        result = Classification("FULL", result.has_docs, result.paths)
    return result


def _strip_fenced_code(text: str) -> str:
    result: list[str] = []
    fence_character: str | None = None
    fence_size = 0
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence_character is None:
                fence_character, fence_size = marker[0], len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_size:
                fence_character, fence_size = None, 0
            continue
        if fence_character is None:
            result.append(line)
    return "\n".join(result)


def markdown_targets(text: str) -> list[str]:
    targets: list[str] = []
    for line in _strip_fenced_code(text).splitlines():
        targets.extend(match.group(1) or match.group(2) for match in _MARKDOWN_LINK.finditer(line))
        reference = _MARKDOWN_REFERENCE.match(line)
        if reference:
            targets.append(reference.group(1) or reference.group(2))
    return targets


def check_markdown_links(root: Path, markdown_path: Path) -> list[str]:
    source = markdown_path if markdown_path.is_absolute() else root / markdown_path
    if not source.exists():
        return []
    problems: list[str] = []
    for target in markdown_targets(source.read_text(encoding="utf-8")):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        target_path = unquote(parsed.path)
        if target_path.startswith("/"):
            resolved = root / target_path.lstrip("/")
        elif target_path:
            resolved = source.parent / target_path
        else:
            resolved = source
        if not resolved.exists():
            problems.append(f"{source.relative_to(root)} -> {target}")
    return problems


def run_doc_checks(changes: list[ChangedPath], diff_args: list[str] | None) -> int:
    if diff_args:
        result = subprocess.run(["git", "diff", "--check", *diff_args], check=False)
        if result.returncode != 0:
            return result.returncode
    root = Path(__file__).resolve().parents[2]
    markdown_files = {
        change.path
        for change in changes
        if _is_markdown(change.path) and (root / change.path).exists()
    }
    problems = [
        problem
        for markdown_path in sorted(markdown_files)
        for problem in check_markdown_links(root, Path(markdown_path))
    ]
    if problems:
        print("Broken repository-relative Markdown link target(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"Documentation checks passed ({len(markdown_files)} changed Markdown file(s)).")
    return 0


def required_jobs(lane: str) -> tuple[str, ...] | None:
    return _REQUIRED_JOBS.get(lane)


def gate_errors(
    lane: str,
    has_docs: bool,
    docs_result: str,
    results: dict[str, str],
) -> list[str]:
    expected = required_jobs(lane)
    if expected is None:
        return [f"unknown or missing classification: {lane!r}"]
    errors: list[str] = []
    if results.get("classify") != "success":
        errors.append(f"classify/security result was {results.get('classify', 'missing')}")
    if lane == "DOCS_ONLY" and not has_docs:
        errors.append("DOCS_ONLY classification had no changed Markdown file")
    if has_docs and docs_result != "success":
        errors.append(f"documentation check result was {docs_result or 'missing'}")
    for job in expected:
        if results.get(job) != "success":
            errors.append(f"required {job} job result was {results.get(job, 'missing')}")
    return errors


def _test_files(suite: str, test_root: Path) -> list[Path]:
    if suite == "api":
        names = (*API_TESTS, *SHARED_TESTS)
    elif suite == "browser":
        names = (*BROWSER_TESTS, *SHARED_TESTS)
    elif suite == "windows-smoke":
        names = (*WINDOWS_SHARED_SMOKE_TESTS, *WINDOWS_API_SMOKE_TESTS)
    elif suite == "windows-browser":
        names = (*WINDOWS_SHARED_SMOKE_TESTS, *BROWSER_TESTS)
    elif suite in {"full", "windows-full"}:
        return [test_root]
    else:
        raise ValueError(f"unknown test suite: {suite}")
    return [test_root / name for name in dict.fromkeys(names)]


def _test_manifest_errors(repo_root: Path) -> list[str]:
    test_root = repo_root / "tests"
    if not test_root.is_dir():
        return ["tests directory is missing"]

    configured = set(_TEST_LANES)
    discovered = {
        path.relative_to(repo_root).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in test_root.rglob(pattern)
        if path.is_file()
    }
    missing = sorted(configured - discovered)
    unclassified = sorted(discovered - configured)
    errors = []
    if missing:
        errors.append(f"configured test modules are missing: {', '.join(missing)}")
    if unclassified:
        errors.append(f"unclassified test modules were discovered: {', '.join(unclassified)}")
    return errors


def run_tests(suite: str) -> int:
    suite = {
        "API_ONLY": "api",
        "BROWSER_ONLY": "browser",
        "SHARED": "full",
        "FULL": "full",
    }.get(suite, suite)
    repo_root = Path(__file__).resolve().parents[2]
    manifest_errors = _test_manifest_errors(repo_root)
    if manifest_errors:
        print("Test manifest verification failed:", *manifest_errors, sep="\n  ", file=sys.stderr)
        return 2

    test_root = repo_root / "tests"
    paths = _test_files(suite, test_root)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print("Configured test file(s) missing:", *missing, sep="\n  ", file=sys.stderr)
        return 2
    print(f"Running {suite} suite ({len(paths)} test file(s)) with pytest durations enabled.")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *map(str, paths),
            "-ra",
            "--durations=30",
            "--durations-min=0.5",
        ],
        check=False,
    ).returncode


def junit_counts(root: ET.Element) -> dict[str, int]:
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "skipped", "failures", "errors")
    }


def run_wheel_smoke(junit_path: Path) -> int:
    if os.environ.get("THREADS_STANDALONE_WHEEL_SMOKE") != "1":
        print("wheel smoke mode is not enabled", file=sys.stderr)
        return 2
    wheel_value = os.environ.get("THREADS_STANDALONE_WHEEL_PATH")
    if not wheel_value or not Path(wheel_value).is_file():
        print("THREADS_STANDALONE_WHEEL_PATH does not name a built wheel", file=sys.stderr)
        return 2
    if os.environ.get("PYTHONPATH"):
        print("PYTHONPATH must be unset for the installed-wheel smoke", file=sys.stderr)
        return 2

    test_root = Path(__file__).resolve().parents[2] / "tests"
    selected_tests = (
        test_root / "test_clean_package.py",
        test_root / "test_cli_smoke.py",
        test_root / "test_browser_mvp_integration.py",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *map(str, selected_tests),
            "-ra",
            "--durations=30",
            "--durations-min=0.5",
            "--basetemp",
            str(junit_path.parent / "pytest-wheel-smoke"),
            f"--junitxml={junit_path}",
        ],
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    if not junit_path.is_file():
        print("pytest did not write the wheel-smoke report", file=sys.stderr)
        return 2
    counts = junit_counts(ET.parse(junit_path).getroot())
    tests = counts["tests"]
    skipped = counts["skipped"]
    failures = counts["failures"]
    errors = counts["errors"]
    print(
        "Installed-wheel smoke results: "
        f"tests={tests}, skipped={skipped}, failures={failures}, errors={errors}."
    )
    if tests == 0 or skipped or failures or errors:
        print(
            "Installed-wheel smoke requires executed tests and zero skips/failures.",
            file=sys.stderr,
        )
        return 1
    return 0


def _gate_from_environment() -> int:
    lane = os.environ.get("CI_LANE", "")
    has_docs = os.environ.get("CI_HAS_DOCS", "").casefold() == "true"
    docs_result = os.environ.get("CI_DOCS_RESULT", "")
    results = {
        "classify": os.environ.get("CI_CLASSIFY_RESULT", ""),
        "linux": os.environ.get("CI_LINUX_RESULT", ""),
        "windows_smoke": os.environ.get("CI_WINDOWS_SMOKE_RESULT", ""),
        "windows_browser": os.environ.get("CI_WINDOWS_BROWSER_RESULT", ""),
        "windows_full": os.environ.get("CI_WINDOWS_FULL_RESULT", ""),
    }
    errors = gate_errors(lane, has_docs, docs_result, results)
    if errors:
        print("Required CI gate failed:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    print(f"Required CI gate passed for {lane}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Actions routing and test helpers.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("classify", help="classify changed files and write GitHub outputs")
    commands.add_parser("docs", help="check changed Markdown links and diff whitespace")
    commands.add_parser("gate", help="enforce the selected lane's required job results")
    test_parser = commands.add_parser("run-tests", help="run a named pytest suite")
    test_parser.add_argument(
        "suite",
        choices=(
            "api",
            "browser",
            "full",
            "windows-smoke",
            "windows-browser",
            "windows-full",
            "API_ONLY",
            "BROWSER_ONLY",
            "SHARED",
            "FULL",
        ),
    )
    smoke_parser = commands.add_parser("wheel-smoke", help="run strict installed-wheel acceptance")
    smoke_parser.add_argument("junit_path", type=Path)
    args = parser.parse_args()

    if args.command == "classify":
        result = route_changes()
        _write_github_output(
            {"lane": result.lane, "has_docs": "true" if result.has_docs else "false"}
        )
        summary = ", ".join(result.paths) if result.paths else "no diff; safe fallback"
        print(f"CI lane: {result.lane}; changed paths: {summary}")
        return 0
    if args.command == "docs":
        return run_doc_checks(_changed_paths(_event_diff_args()), _event_diff_args())
    if args.command == "gate":
        return _gate_from_environment()
    if args.command == "run-tests":
        return run_tests(args.suite)
    return run_wheel_smoke(args.junit_path)


if __name__ == "__main__":
    raise SystemExit(main())
