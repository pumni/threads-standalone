from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sys
from pathlib import Path

import pytest

import threads_platform.standalone.__main__ as cli
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.manual_opportunities import (
    ManualOpportunity,
    build_manual_draft_review,
    load_manual_opportunities,
)
from threads_platform.standalone.manual_outcomes import ManualOutcomeStore
from threads_platform.standalone.opportunity_queue import (
    OpportunityQueueError,
    OpportunityQueueStore,
)


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def _write_text(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _write_bytes(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _target(post_id: str, author: str = "synthetic") -> str:
    return f"https://www.threads.com/@{author}/post/{post_id}"


def _queue_path(root: Path, account: LocalAccount) -> Path:
    return root / "opportunity_queue_v1" / "accounts" / f"{account.id}.json"


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    arguments: list[str],
    *,
    input_text: str = "",
    tty: bool = True,
) -> tuple[int, str, str]:
    stdin: io.StringIO = _TtyStringIO(input_text) if tty else io.StringIO(input_text)
    stdout: io.StringIO = _TtyStringIO() if tty else io.StringIO()
    stderr: io.StringIO = _TtyStringIO() if tty else io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return cli.main(arguments), stdout.getvalue(), stderr.getvalue()


def _add_account(root: Path, alias: str = "synthetic_account") -> LocalAccount:
    return LocalAccountStore(root).add(alias)


@pytest.mark.parametrize(
    ("count", "expected_code"),
    (
        (0, "MANUAL_TARGET_COUNT_INVALID"),
        (1, None),
        (20, None),
        (21, "MANUAL_TARGET_COUNT_INVALID"),
    ),
)
def test_import_enforces_zero_one_twenty_and_twenty_one_url_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    expected_code: str | None,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    targets = _write_text(
        tmp_path / "targets.txt",
        "\n".join(_target(f"s6_bound_{index}") for index in range(count)),
    )

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
        ],
    )

    assert stdout == ""
    if expected_code is None:
        assert result == 0
        queue = OpportunityQueueStore(root).list(account.id)
        assert len(queue.opportunities) == count
        assert queue.active_count == count
        assert f"added={count}" in stderr
    else:
        assert result == 1
        assert f"ERROR {expected_code}" in stderr
        assert not _queue_path(root, account).exists()


def test_import_deduplicates_tracking_urls_and_existing_queue_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_duplicate_1")
    first = load_manual_opportunities(
        _write_text(
            tmp_path / "first.txt",
            f"{target}?utm_source=operator\n{target}?xmt=copy\n",
        )
    )
    store = OpportunityQueueStore(root)

    imported = store.import_candidates(account.id, first)
    repeated = store.import_candidates(
        account.id,
        load_manual_opportunities(
            _write_text(tmp_path / "second.txt", f"{target}?igsh=synthetic\n")
        ),
    )

    queue = OpportunityQueueStore(root).list(account.id)
    assert imported.added_count == 1
    assert imported.duplicate_count == 1
    assert repeated.added_count == 0
    assert repeated.duplicate_count == 1
    assert len(queue.opportunities) == 1
    assert queue.opportunities[0].target_url == target


@pytest.mark.parametrize(
    ("content", "expected_code"),
    (
        (b"https://threads.net/@synthetic/post/foreign_1\n", "MANUAL_TARGET_URL_INVALID"),
        (b"https://www.threads.com/not_a_post\n", "MANUAL_TARGET_URL_INVALID"),
        (b"https://www.threads.com/@synthetic/post/\xff\n", "MANUAL_TARGET_ENCODING_INVALID"),
        (b"x" * (64 * 1024 + 1), "MANUAL_TARGET_FILE_TOO_LARGE"),
    ),
    ids=("foreign_host", "invalid_path", "invalid_utf8", "oversized_file"),
)
def test_import_rejects_foreign_host_invalid_encoding_and_oversized_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    expected_code: str,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    targets = _write_bytes(tmp_path / "targets.txt", content)

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
        ],
    )

    assert result == 1
    assert stdout == ""
    assert f"ERROR {expected_code}" in stderr
    assert not _queue_path(root, account).exists()


def test_queue_state_corruption_and_oversized_state_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    store = OpportunityQueueStore(root)
    intake = load_manual_opportunities(
        _write_text(tmp_path / "targets.txt", _target("s6_corrupt_1"))
    )
    store.import_candidates(account.id, intake)
    path = _queue_path(root, account)

    for corrupt in (b'{"version":1,"version":1}', b"{" + b" " * (1024 * 1024)):
        path.write_bytes(corrupt)
        with pytest.raises(OpportunityQueueError) as invalid:
            store.list(account.id)
        assert invalid.value.code == "OPPORTUNITY_QUEUE_INVALID"


def test_import_rejects_oversized_context_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    targets = _write_text(tmp_path / "targets.txt", _target("s6_context_oversized"))
    context = _write_bytes(tmp_path / "context.txt", b"x" * (16 * 1024 + 1))

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
            "--context-file",
            str(context),
        ],
    )

    assert result == 1
    assert stdout == ""
    assert "ERROR MANUAL_CONTEXT_CONTENT_INVALID" in stderr
    assert not _queue_path(root, account).exists()


def test_queue_restart_durability_and_two_account_isolation(tmp_path: Path) -> None:
    root = tmp_path / "local-data"
    first_account = _add_account(root, "synthetic_first")
    second_account = _add_account(root, "synthetic_second")
    store = OpportunityQueueStore(root)
    first_target = _target("s6_first_account")
    second_target = _target("s6_second_account")
    store.import_candidates(
        first_account.id,
        load_manual_opportunities(_write_text(tmp_path / "first.txt", first_target)),
    )
    store.import_candidates(
        second_account.id,
        load_manual_opportunities(_write_text(tmp_path / "second.txt", second_target)),
    )

    restarted = OpportunityQueueStore(root)
    first_queue = restarted.list(first_account.id)
    second_queue = restarted.list(second_account.id)

    assert first_queue.account_uuid == str(first_account.id)
    assert [entry.target_url for entry in first_queue.opportunities] == [first_target]
    assert second_queue.account_uuid == str(second_account.id)
    assert [entry.target_url for entry in second_queue.opportunities] == [second_target]
    assert _queue_path(root, first_account).parent == _queue_path(root, second_account).parent
    assert _queue_path(root, first_account) != _queue_path(root, second_account)


def test_twenty_active_limit_is_atomic_and_skipped_items_free_capacity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    store = OpportunityQueueStore(root)
    initial_targets = _write_text(
        tmp_path / "initial.txt",
        "\n".join(_target(f"s6_active_{index}") for index in range(20)),
    )
    store.import_candidates(account.id, load_manual_opportunities(initial_targets))
    before = store.list(account.id)
    extra_target = load_manual_opportunities(
        _write_text(tmp_path / "extra.txt", _target("s6_active_extra"))
    )

    with pytest.raises(OpportunityQueueError) as full:
        store.import_candidates(account.id, extra_target)
    assert full.value.code == "OPPORTUNITY_QUEUE_LIMIT_REACHED"
    assert store.list(account.id) == before

    store.skip(account.id, before.opportunities[0].id)
    added = store.import_candidates(account.id, extra_target)
    after = store.list(account.id)
    assert added.added_count == 1
    assert after.active_count == 20
    assert len(after.opportunities) == 21


def test_account_lock_contention_is_bounded_and_isolated(tmp_path: Path) -> None:
    root = tmp_path / "local-data"
    first_account = _add_account(root, "synthetic_first")
    second_account = LocalAccountStore(root).add("synthetic_second")
    store = OpportunityQueueStore(root)

    with store.account_lock(first_account.id):
        with pytest.raises(OpportunityQueueError) as busy:
            store.list(first_account.id)
        assert busy.value.code == "OPPORTUNITY_QUEUE_BUSY"
        assert store.list(second_account.id).opportunities == ()


def test_persistence_is_private_and_cli_output_stays_on_private_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_private_1")
    targets = _write_text(tmp_path / "targets.txt", target)
    context = "Synthetic operator context that stays local."
    context_file = _write_text(tmp_path / "context.txt", context)

    result, stdout, imported_stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
            "--context-file",
            str(context_file),
        ],
    )
    entry = OpportunityQueueStore(root).list(account.id).opportunities[0]
    listed_result, listed_stdout, listed_stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-list", account.alias],
    )

    assert result == listed_result == 0
    assert stdout == listed_stdout == ""
    assert "OPPORTUNITY QUEUE IMPORT" in imported_stderr
    assert target in listed_stderr
    assert context in listed_stderr
    assert entry.context_provenance == "OPERATOR_SUPPLIED"
    if os.name != "nt":
        assert os.stat(_queue_path(root, account)).st_mode & 0o777 == 0o600


def test_queue_cli_requires_private_tty_before_resolving_the_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "must-not-be-created"
    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-list", "synthetic_account"],
        tty=False,
    )

    assert result == 1
    assert stdout == ""
    assert "ERROR PRIVATE_REVIEW_TERMINAL_REQUIRED" in stderr
    assert not root.exists()


def test_select_reuses_s3_review_with_exact_queue_target_context_and_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_review_1")
    targets = _write_text(tmp_path / "targets.txt", target)
    context = "Synthetic context supplied by the operator."
    context_file = _write_text(tmp_path / "context.txt", context)
    draft = "Synthetic operator-authored recruitment draft."
    draft_file = _write_text(tmp_path / "draft.txt", draft)

    imported, _, _ = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
        ],
    )
    entry = OpportunityQueueStore(root).list(account.id).opportunities[0]
    selected, stdout, stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-select",
            account.alias,
            "--id",
            entry.id,
            "--confirm-target",
            target,
            "--context-file",
            str(context_file),
            "--context-completeness",
            "COMPLETE",
            "--draft-file",
            str(draft_file),
        ],
    )

    updated = OpportunityQueueStore(root).get(account.id, entry.id)
    expected = build_manual_draft_review(
        account,
        ManualOpportunity(1, 1, target, "synthetic"),
        draft,
        context,
        context_completeness="COMPLETE",
    )
    expected_sha = hashlib.sha256(
        json.dumps(
            [str(account.id), target, draft],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert imported == selected == 0
    assert stdout == ""
    assert "MANUAL OPPORTUNITY REVIEW — PRIVATE LOCAL TERMINAL" in stderr
    assert "This queue item was read or updated locally" in stderr
    assert f"account UUID: {account.id}" in stderr
    assert f"selected target: {target}" in stderr
    assert "context completeness: COMPLETE" in stderr
    assert json.dumps(context) in stderr
    assert json.dumps(draft) in stderr
    assert expected.draft_sha256 == expected_sha
    assert f"draft sha256: {expected_sha}" in stderr
    assert updated.state == "READY"
    assert updated.context == context
    assert updated.context_provenance == "OPERATOR_SUPPLIED"


def test_s4_unresolved_outcome_still_blocks_handoff_for_queued_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_unresolved_1")
    target_file = _write_text(tmp_path / "targets.txt", target)
    draft = "Synthetic approved draft."
    draft_file = _write_text(tmp_path / "draft.txt", draft)
    selected = ManualOpportunity(1, 1, target, "synthetic")
    approval_sha = build_manual_draft_review(account, selected, draft, None).draft_sha256
    outcomes = ManualOutcomeStore(root)
    pending = outcomes.begin_handoff(account.id, target, approval_sha)
    queue = OpportunityQueueStore(root)
    queue.import_candidates(account.id, load_manual_opportunities(target_file))
    entry = queue.list(account.id).opportunities[0]

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "manual-handoff",
            account.alias,
            "--targets-file",
            str(target_file),
            "--select",
            "1",
            "--confirm-target",
            target,
            "--draft-file",
            str(draft_file),
            "--approve-sha",
            approval_sha,
            "--interactive",
        ],
        input_text="OPEN\n",
    )

    assert result == 1
    assert stdout == ""
    assert "ERROR MANUAL_OUTCOME_BLOCKED" in stderr
    assert "browser" not in stderr.casefold()
    assert pending.status == "HANDOFF_UNRESOLVED"
    assert queue.get(account.id, entry.id).target_url == target


def test_skip_releases_active_capacity_and_remove_requires_literal_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    store = OpportunityQueueStore(root)
    first_target = _target("s6_skip_1")
    store.import_candidates(
        account.id,
        load_manual_opportunities(_write_text(tmp_path / "first.txt", first_target)),
    )
    first_entry = store.list(account.id).opportunities[0]
    result, _, skipped_output = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-skip", account.alias, "--id", first_entry.id],
    )
    assert result == 0
    assert "state=SKIPPED" in skipped_output

    next_target = _target("s6_skip_2")
    store.import_candidates(
        account.id,
        load_manual_opportunities(_write_text(tmp_path / "second.txt", next_target)),
    )
    next_entry = next(
        entry for entry in store.list(account.id).opportunities if entry.target_url == next_target
    )
    removed, stdout, output = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-remove", account.alias, "--id", first_entry.id],
        input_text="DELETE\n",
    )

    assert removed == 0
    assert stdout == ""
    assert "opportunity removed" in output
    assert [entry.id for entry in store.list(account.id).opportunities] == [next_entry.id]


def test_remove_refuses_to_delete_target_with_unresolved_s4_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_remove_pending")
    store = OpportunityQueueStore(root)
    store.import_candidates(
        account.id,
        load_manual_opportunities(_write_text(tmp_path / "targets.txt", target)),
    )
    entry = store.list(account.id).opportunities[0]
    ManualOutcomeStore(root).begin_handoff(account.id, target, "a" * 64)

    result, stdout, stderr = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-remove", account.alias, "--id", entry.id],
        input_text="DELETE\n",
    )

    assert result == 1
    assert stdout == ""
    assert "ERROR OPPORTUNITY_QUEUE_HANDOFF_UNRESOLVED" in stderr
    assert store.get(account.id, entry.id).target_url == target


def test_wrong_alias_and_wrong_target_fail_without_leaking_private_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_wrong_alias")
    targets = _write_text(tmp_path / "targets.txt", target)

    wrong_alias, stdout, alias_error = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            "unknown_alias",
            "--targets-file",
            str(targets),
        ],
    )
    imported, _, _ = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
        ],
    )
    entry = OpportunityQueueStore(root).list(account.id).opportunities[0]
    wrong_target, wrong_target_stdout, target_error = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-select",
            account.alias,
            "--id",
            entry.id,
            "--confirm-target",
            _target("s6_not_same"),
        ],
    )

    assert wrong_alias == 1
    assert "ERROR ACCOUNT_NOT_FOUND" in alias_error
    assert stdout == ""
    assert imported == 0
    assert wrong_target == 1
    assert wrong_target_stdout == ""
    assert "ERROR MANUAL_TARGET_MISMATCH" in target_error
    assert target not in target_error


def test_queue_operations_have_no_network_or_runtime_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-data"
    account = _add_account(root)
    target = _target("s6_offline_1")
    targets = _write_text(tmp_path / "targets.txt", target)
    context = _write_text(tmp_path / "context.txt", "Synthetic observed context.")
    draft = _write_text(tmp_path / "draft.txt", "Synthetic operator draft.")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("queue commands must not construct a remote, browser, or mutation runtime")

    monkeypatch.setattr(cli, "_run_browser_command", forbidden)
    monkeypatch.setattr(cli, "_run_api_command", forbidden)
    monkeypatch.setattr(cli, "_handle_nurture_command", forbidden)
    monkeypatch.setattr(cli, "_run_post_command", forbidden)
    monkeypatch.setattr(cli, "_run_reply_command", forbidden)
    monkeypatch.setattr(cli, "build_standalone_app", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    imported, _, _ = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-import",
            account.alias,
            "--targets-file",
            str(targets),
        ],
    )
    entry = OpportunityQueueStore(root).list(account.id).opportunities[0]
    listed, _, _ = _invoke(
        monkeypatch,
        root,
        ["recruitment", "opportunity-list", account.alias],
    )
    selected, _, _ = _invoke(
        monkeypatch,
        root,
        [
            "recruitment",
            "opportunity-select",
            account.alias,
            "--id",
            entry.id,
            "--confirm-target",
            target,
            "--context-file",
            str(context),
            "--draft-file",
            str(draft),
        ],
    )

    assert imported == listed == selected == 0
