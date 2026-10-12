from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from uuid import UUID

import pytest

import threads_platform.standalone.__main__ as cli
from threads_platform.standalone import manual_opportunities
from threads_platform.standalone.accounts import (
    LocalAccount,
    LocalAccountStore,
)
from threads_platform.standalone.manual_opportunities import (
    ManualOpportunityError,
    build_manual_draft_review,
    load_manual_opportunities,
    load_operator_context,
    load_operator_draft,
    run_manual_review_cli,
    s1_relative_thread_ref,
)


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def _install_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_TtyStringIO, _TtyStringIO, _TtyStringIO]:
    stdin = _TtyStringIO()
    stdout = _TtyStringIO()
    stderr = _TtyStringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return stdin, stdout, stderr


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _target(post_id: str, author: str = "synthetic") -> str:
    return f"https://www.threads.com/@{author}/post/{post_id}"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_manual_review_cli_runs_offline_and_keeps_state_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "local-data"
    account = LocalAccountStore(data_root).add("synthetic_account")
    (data_root / "nurture").mkdir()
    (data_root / "nurture" / "synthetic-state.json").write_text(
        '{"fixture":"unchanged"}\n', encoding="utf-8"
    )
    (data_root / "operations").mkdir()
    (data_root / "operations" / "synthetic-journal.json").write_text(
        '{"fixture":"unchanged"}\n', encoding="utf-8"
    )
    before = _snapshot(data_root)

    first_target = _target("first_1")
    selected_target = _target("selected_2", "second")
    targets_file = _write(
        tmp_path / "targets.txt",
        "  HTTPS://WWW.THREADS.COM/@Synthetic/post/first_1?utm_source=operator  \n"
        "https://www.threads.com/@synthetic/post/first_1?xmt=copy\n"
        f"{selected_target}\n",
    )
    draft = "Hello, I saw your post and wanted to share the role details."
    draft_file = _write(tmp_path / "draft.txt", draft)
    context = "Operator observed that the author asked about the role."
    context_file = _write(tmp_path / "context.txt", context)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(data_root))
    _install_tty(monkeypatch)

    def unexpected_runtime(*_args: object, **_kwargs: object) -> object:
        pytest.fail("manual review must not construct browser, API, nurture, or mutation runtimes")

    monkeypatch.setattr(cli, "_run_browser_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_run_api_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_handle_nurture_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_run_post_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_run_reply_command", unexpected_runtime)

    assert (
        cli.main(
            [
                "recruitment",
                "manual-review",
                "synthetic_account",
                "--targets-file",
                str(targets_file),
                "--select",
                "2",
                "--draft-file",
                str(draft_file),
                "--context-file",
                str(context_file),
            ]
        )
        == 0
    )

    stderr = sys.stderr.getvalue()
    assert sys.stdout.getvalue() == ""
    assert (
        f"1. input_position=1 source_line=1 status=NEEDS_CONTEXT provenance=MANUAL_URL target={first_target}"
        in stderr
    )
    assert "duplicate source_line=2 maps_to_selection=1 reason=DUPLICATE" in stderr
    assert (
        f"2. input_position=3 source_line=3 "
        f"status=OPERATOR_CONTEXT_SUPPLIED_REVIEW_REQUIRED provenance=MANUAL_URL "
        f"target={selected_target}" in stderr
    )
    assert "selected shortlist item: 2" in stderr
    assert f"selected target: {selected_target}" in stderr
    assert "target author from permalink only: second (identity unverified)" in stderr
    assert "actor identity: UNKNOWN_IDENTITY" in stderr
    assert "context provenance: OPERATOR_SUPPLIED" in stderr
    assert "context completeness: UNKNOWN" in stderr
    assert json.dumps(context, ensure_ascii=False) in stderr
    assert json.dumps(draft, ensure_ascii=False) in stderr
    expected_hash = hashlib.sha256(
        json.dumps(
            [str(account.id), selected_target, draft],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert f"draft sha256: {expected_hash}" in stderr
    assert "status: NOT_SENT" in stderr
    assert (
        'threads-local browser open-thread synthetic_account "/@second/post/selected_2" --interactive'
        in stderr
    )
    assert _snapshot(data_root) == before


def test_manual_review_without_context_keeps_candidate_needs_context(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path / "local-data")
    account = store.add("synthetic_account")
    targets = _write(tmp_path / "targets.txt", _target("fake_1"))
    draft = _write(tmp_path / "draft.txt", "Synthetic operator-authored draft")
    stdin, stdout, stderr = _TtyStringIO(), _TtyStringIO(), _TtyStringIO()

    assert (
        run_manual_review_cli(
            account.alias,
            targets,
            1,
            draft,
            None,
            account_store_factory=lambda: store,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
        == 0
    )

    assert "status=NEEDS_CONTEXT" in stderr.getvalue()
    assert "context provenance: NONE" in stderr.getvalue()
    assert "context completeness: UNKNOWN" in stderr.getvalue()
    assert "operator context: <not supplied>" in stderr.getvalue()
    assert stdout.getvalue() == ""


def test_manual_review_optional_exact_target_confirmation_fails_closed(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path / "local-data")
    account = store.add("synthetic_account")
    target_url = _target("AbC")
    targets = _write(tmp_path / "targets.txt", target_url)
    draft = _write(tmp_path / "draft.txt", "Synthetic private draft")
    stdin, stdout, stderr = _TtyStringIO(), _TtyStringIO(), _TtyStringIO()

    with pytest.raises(ManualOpportunityError) as mismatch:
        run_manual_review_cli(
            account.alias,
            targets,
            1,
            draft,
            None,
            account_store_factory=lambda: store,
            confirm_target=_target("abc"),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
    assert mismatch.value.code == "MANUAL_TARGET_MISMATCH"
    assert stderr.getvalue() == ""

    assert (
        run_manual_review_cli(
            account.alias,
            targets,
            1,
            draft,
            None,
            account_store_factory=lambda: store,
            confirm_target=target_url,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            environment={},
        )
        == 0
    )
    assert f"selected target: {target_url}" in stderr.getvalue()


def test_manual_list_shows_canonical_shortlist_privately_without_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "local-data"
    store = LocalAccountStore(data_root)
    store.add("synthetic_account")
    before = _snapshot(data_root)
    first_target = _target("AbC")
    second_target = _target("abc")
    targets = _write(
        tmp_path / "targets.txt",
        "HTTPS://WWW.THREADS.COM/@Synthetic/post/AbC?utm_source=operator\n"
        "https://www.threads.com/@synthetic/post/AbC?xmt=copy\n"
        f"{second_target}\n",
    )
    _install_tty(monkeypatch)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(data_root))

    def unexpected_runtime(*_args: object, **_kwargs: object) -> object:
        pytest.fail("manual list must not construct browser, API, or mutation runtimes")

    monkeypatch.setattr(cli, "_run_browser_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_run_api_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_run_post_command", unexpected_runtime)
    monkeypatch.setattr(cli, "_run_reply_command", unexpected_runtime)

    assert (
        cli.main(
            [
                "recruitment",
                "manual-list",
                "synthetic_account",
                "--targets-file",
                str(targets),
            ]
        )
        == 0
    )

    assert sys.stdout.getvalue() == ""
    assert sys.stderr.getvalue() == (
        "MANUAL OPPORTUNITY SHORTLIST — PRIVATE LOCAL TERMINAL\n"
        f"1. source_line=1 target={first_target}\n"
        f"2. source_line=3 target={second_target}\n"
        "duplicate source_line=2 maps_to_selection=1 reason=DUPLICATE\n"
    )
    assert _snapshot(data_root) == before


def test_manual_dedup_preserves_post_id_case_and_normalizes_author_and_tracking(
    tmp_path: Path,
) -> None:
    targets = _write(
        tmp_path / "targets.txt",
        "HTTPS://WWW.THREADS.COM/@Synthetic/post/AbC?utm_source=operator\n"
        "https://www.threads.com/@synthetic/post/AbC?xmt=copy\n"
        "https://www.threads.com/@synthetic/post/abc\n",
    )

    intake = load_manual_opportunities(targets)

    assert [candidate.target_url for candidate in intake.candidates] == [
        _target("AbC"),
        _target("abc"),
    ]
    assert len(intake.duplicates) == 1
    assert intake.duplicates[0].source_line == 2
    assert intake.duplicates[0].shortlist_index == 1


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "MANUAL_TARGET_COUNT_INVALID"),
        (1, None),
        (20, None),
        (21, "MANUAL_TARGET_COUNT_INVALID"),
    ],
)
def test_manual_target_input_count_is_bounded(
    tmp_path: Path, count: int, expected: str | None
) -> None:
    target_file = tmp_path / "targets.txt"
    _write(
        target_file,
        "\n" if count == 0 else "\n".join(_target(f"post_{index}") for index in range(count)),
    )
    if expected is None:
        assert len(load_manual_opportunities(target_file).candidates) == count
    else:
        with pytest.raises(ManualOpportunityError) as error:
            load_manual_opportunities(target_file)
        assert error.value.code == expected


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("invalid_utf8", "MANUAL_TARGET_ENCODING_INVALID"),
        ("bom", "MANUAL_TARGET_ENCODING_INVALID"),
        ("oversized", "MANUAL_TARGET_FILE_TOO_LARGE"),
    ],
)
def test_target_file_rejects_bad_encoding_bom_and_oversized_file(
    tmp_path: Path, case: str, expected: str
) -> None:
    target_file = tmp_path / "targets.txt"
    contents = {
        "invalid_utf8": b"\xff",
        "bom": b"\xef\xbb\xbfhttps://www.threads.com/@synthetic/post/fake_1",
        "oversized": b"https://www.threads.com/@synthetic/post/fake_1\n" * 1400,
    }[case]
    target_file.write_bytes(contents)
    with pytest.raises(ManualOpportunityError) as error:
        load_manual_opportunities(target_file)
    assert error.value.code == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://www.threads.com.evil/@synthetic/post/fake_1",
        "https://www.threads.com@evil.invalid/@synthetic/post/fake_1",
        "https://www.threads.com/@synthetic/post/fake_1?redirect=https://evil.invalid",
        "https://www.threads.com/@synthetic/post/fake_1\u202e",
        "https://www.threads.com/@synthetic/post/" + "x" * 2049,
    ],
)
def test_manual_target_rejects_host_tricks_controls_and_oversized_url(
    tmp_path: Path, value: str
) -> None:
    target_file = _write(tmp_path / "targets.txt", value)
    with pytest.raises(ManualOpportunityError) as error:
        load_manual_opportunities(target_file)
    assert error.value.code == "MANUAL_TARGET_URL_INVALID"


@pytest.mark.parametrize("value", ["", "\ufeffdraft", "x" * 501])
def test_operator_draft_requires_unambiguous_bounded_utf8(tmp_path: Path, value: str) -> None:
    draft_file = _write(tmp_path / "draft.txt", value)
    with pytest.raises(ManualOpportunityError) as error:
        load_operator_draft(draft_file)
    assert error.value.code in {
        "MANUAL_DRAFT_ENCODING_INVALID",
        "MANUAL_DRAFT_CONTENT_INVALID",
    }


@pytest.mark.parametrize(
    "value", ["draft\x00text", "draft\u202etext", "draft\u200btext", "draft\u2066text"]
)
def test_operator_draft_rejects_controls_and_invisible_spoofing(tmp_path: Path, value: str) -> None:
    draft_file = _write(tmp_path / "draft.txt", value)
    with pytest.raises(ManualOpportunityError) as error:
        load_operator_draft(draft_file)
    assert error.value.code == "MANUAL_DRAFT_CONTENT_INVALID"


def test_missing_draft_and_context_limits_fail_with_safe_codes(tmp_path: Path) -> None:
    with pytest.raises(ManualOpportunityError) as missing:
        load_operator_draft(tmp_path / "missing.txt")
    assert missing.value.code == "MANUAL_DRAFT_FILE_INVALID"

    invalid_context = _write(tmp_path / "context.txt", "context\u202evalue")
    with pytest.raises(ManualOpportunityError) as unsafe:
        load_operator_context(invalid_context)
    assert unsafe.value.code == "MANUAL_CONTEXT_CONTENT_INVALID"

    oversized_context = _write(tmp_path / "large-context.txt", "x" * 4001)
    with pytest.raises(ManualOpportunityError) as too_long:
        load_operator_context(oversized_context)
    assert too_long.value.code == "MANUAL_CONTEXT_CONTENT_INVALID"


def test_selection_alias_and_missing_file_errors_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "local-data"
    store = LocalAccountStore(data_root)
    account = store.add("synthetic_account")
    targets = _write(tmp_path / "targets.txt", _target("fake_1"))
    draft = _write(tmp_path / "draft.txt", "Synthetic private draft")
    _install_tty(monkeypatch)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(data_root))

    assert (
        cli.main(
            [
                "recruitment",
                "manual-review",
                "../invalid",
                "--targets-file",
                str(targets),
                "--select",
                "1",
                "--draft-file",
                str(draft),
            ]
        )
        == 1
    )
    assert "ERROR INVALID_ACCOUNT_ALIAS" in sys.stderr.getvalue()

    with pytest.raises(ManualOpportunityError) as missing_draft:
        run_manual_review_cli(
            account.alias,
            targets,
            1,
            tmp_path / "missing-draft.txt",
            None,
            account_store_factory=lambda: store,
        )
    assert missing_draft.value.code == "MANUAL_DRAFT_FILE_INVALID"

    with pytest.raises(ManualOpportunityError) as bad_selection:
        run_manual_review_cli(
            account.alias,
            targets,
            2,
            draft,
            None,
            account_store_factory=lambda: store,
        )
    assert bad_selection.value.code == "MANUAL_SELECTION_INVALID"


def test_private_terminal_refusal_precedes_account_and_file_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_file = tmp_path / "private-targets.txt"
    draft_file = tmp_path / "private-draft.txt"
    target_file.write_text("PRIVATE_URL_SENTINEL", encoding="utf-8")
    draft_file.write_text("PRIVATE_DRAFT_SENTINEL", encoding="utf-8")
    streams = _install_tty(monkeypatch)

    def unexpected_root() -> Path:
        pytest.fail("TTY gate must precede local account root resolution")

    monkeypatch.setattr(cli, "resolve_standalone_data_root", unexpected_root)
    cases = [
        (io.StringIO(), streams[1], streams[2], {}),
        (streams[0], streams[1], streams[2], {"CI": "true"}),
        (streams[0], streams[1], io.StringIO(), {}),
        (streams[0], streams[1], streams[2], {"GITHUB_ACTIONS": "true"}),
    ]
    commands = [
        [
            "recruitment",
            "manual-review",
            "synthetic_account",
            "--targets-file",
            str(target_file),
            "--select",
            "1",
            "--draft-file",
            str(draft_file),
        ],
        [
            "recruitment",
            "manual-list",
            "synthetic_account",
            "--targets-file",
            str(target_file),
        ],
    ]
    for stdin, stdout, stderr, environment in cases:
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr(manual_opportunities.os, "environ", environment)
        for command in commands:
            assert cli.main(command) == 1
            rendered = stdout.getvalue() + stderr.getvalue()
            assert "PRIVATE_REVIEW_TERMINAL_REQUIRED" in rendered
            assert "PRIVATE_URL_SENTINEL" not in rendered
            assert "PRIVATE_DRAFT_SENTINEL" not in rendered
            stdout.seek(0)
            stdout.truncate(0)
            stderr.seek(0)
            stderr.truncate(0)


def test_hash_binds_account_target_and_normalized_draft(tmp_path: Path) -> None:
    target_file = _write(tmp_path / "targets.txt", _target("fake_1") + "\n" + _target("fake_2"))
    candidates = load_manual_opportunities(target_file).candidates
    first_account = LocalAccount(1, UUID("00000000-0000-0000-0000-000000000001"), "synthetic")
    other_account = LocalAccount(1, UUID("00000000-0000-0000-0000-000000000002"), "synthetic")
    first = build_manual_draft_review(first_account, candidates[0], "draft one", None)
    changed_draft = build_manual_draft_review(first_account, candidates[0], "draft two", None)
    changed_target = build_manual_draft_review(first_account, candidates[1], "draft one", None)
    changed_account = build_manual_draft_review(other_account, candidates[0], "draft one", None)

    assert len({first.draft_sha256, changed_draft.draft_sha256}) == 2
    assert len({first.draft_sha256, changed_target.draft_sha256}) == 2
    assert len({first.draft_sha256, changed_account.draft_sha256}) == 2
    assert first.relative_thread_ref == "/@synthetic/post/fake_1"
    assert "draft one" not in repr(first)
    assert "https://www.threads.com/@synthetic/post/fake_1" not in repr(first)


@pytest.mark.parametrize(
    "value",
    [
        _target("fake_1"),
        "https://www.threads.com/@synthetic/post/fake_1?utm_source=operator",
        "https://www.threads.com.evil/@synthetic/post/fake_1",
        "/@synthetic/post/fake_1",
    ],
)
def test_s1_handoff_accepts_only_the_exact_canonical_s2_target(value: str) -> None:
    if value == _target("fake_1"):
        assert s1_relative_thread_ref(value) == "/@synthetic/post/fake_1"
    else:
        with pytest.raises(ManualOpportunityError) as error:
            s1_relative_thread_ref(value)
        assert error.value.code == "MANUAL_TARGET_MISMATCH"


def test_manual_review_parser_is_additive_and_requires_explicit_selection() -> None:
    parsed = cli._build_parser().parse_args(
        [
            "recruitment",
            "manual-review",
            "synthetic_account",
            "--targets-file",
            "targets.txt",
            "--select",
            "2",
            "--draft-file",
            "draft.txt",
        ]
    )
    assert parsed.recruitment_command == "manual-review"
    assert parsed.select == 2
    assert parsed.context_file is None
    assert parsed.confirm_target is None
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            [
                "recruitment",
                "manual-review",
                "synthetic_account",
                "--targets-file",
                "targets.txt",
                "--draft-file",
                "draft.txt",
            ]
        )

    list_parsed = cli._build_parser().parse_args(
        [
            "recruitment",
            "manual-list",
            "synthetic_account",
            "--targets-file",
            "targets.txt",
        ]
    )
    assert list_parsed.recruitment_command == "manual-list"
    assert list_parsed.targets_file == "targets.txt"
    assert not hasattr(list_parsed, "draft_file")
