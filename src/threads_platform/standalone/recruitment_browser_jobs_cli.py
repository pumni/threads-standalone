"""Redacted foreground CLI for synthetic Browser Jobs observations only.

This adapter accepts a bounded local fixture, turns it into BJ-02 fake session
objects, and delegates to the unchanged BJ-04A watch engine. It has no live
browser, HTTP, account-store, API, process, or engagement implementation.
"""

# BJ-04A/BJ-03 own the private path primitives and remain read-only here.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.browser.job_observation import (
    SYNTHETIC_ORIGIN,
    SYNTHETIC_SEARCH_URL,
    FakeBodyVisibility,
    FakeJobCardV1,
    FakeJobFeedBatchV1,
    FakeJobPostDetailV1,
    FakePageState,
    FakeStopSignal,
    ObservedPostKind,
)
from threads_platform.standalone import recruitment_job_store as _store_fs
from threads_platform.standalone.browser_job_watch import (
    BrowserJobWatchEngine,
    SyntheticCycleInput,
    WatchClock,
    WatchReasonCode,
    WatchRunResult,
    WatchSleeper,
    WatchSource,
    WatchStatus,
)
from threads_platform.standalone.recruitment_job_store import (
    RecruitmentJobStoreError,
    RetentionPolicy,
)

DEFAULT_MAX_CYCLES = 1
MAX_CYCLES = 5
MAX_POSTS_PER_CYCLE = 20
MIN_INTERVAL_MINUTES = 15
MAX_FIXTURE_BYTES = 1_100_000
MAX_POST_BODY_CODEPOINTS = 10_000

_FIXTURE_QUERY = "synthetic recruitment fixture"
_FIXTURE_ROOT_MARKER = ".browser_jobs_synthetic_root_v1"
_FIXTURE_ROOT_MARKER_BYTES = b"BROWSER_JOBS_SYNTHETIC_ROOT_V1\n"
_ALLOWED_ROOT_ENTRIES = frozenset(
    {_FIXTURE_ROOT_MARKER, "browser_job_watch_v1", "job_vacancies_v1"}
)
_ACCOUNT_ALIAS_RE = re.compile(r"^(?:test|synthetic)-[a-z0-9](?:[a-z0-9-]{0,29})$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_POST_URL_RE = re.compile(
    r"^https://threads\.example\.invalid/@[A-Za-z0-9._:-]{1,96}/"
    r"[A-Za-z0-9._:-]{1,128}$"
)
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w])(?:\+?84|0)(?:[ \t().-]*\d){8,10}(?!\w)")
_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"']+")
_SAFE_SYNTHETIC_CONTACT_RE = re.compile(
    r"\[(?:SYNTHETIC_(?:PHONE|CONTACT)|FAKE_CONTACT)_[0-9]{3}\]"
)


class BrowserJobsCliError(Exception):
    """Fixed-code failure that never contains an input value or path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"BrowserJobsCliError({self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class _FixturePost:
    url: str = field(repr=False)
    body: str | None = field(repr=False)
    visibility: FakeBodyVisibility
    published_at_utc: datetime | None = field(repr=False)

    def __repr__(self) -> str:
        return "_FixturePost(values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _FixtureCycle:
    targets: tuple[str, ...] = field(repr=False)
    posts: tuple[_FixturePost, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"_FixtureCycle(target_count={len(self.targets)})"


class _FixtureSession:
    """In-memory BJ-02 session with no navigation or external I/O methods."""

    def __init__(self, batch: FakeJobFeedBatchV1, details: dict[str, FakeJobPostDetailV1]):
        self._batch = batch
        self._details = details

    def __repr__(self) -> str:
        return "_FixtureSession(values=<redacted>)"

    def read_initial_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        if (
            account_uuid != self._batch.account_uuid
            or query != self._batch.query
            or max_cards < len(self._batch.cards)
        ):
            raise ValueError("FIXTURE_SESSION_MISMATCH")
        return self._batch

    def scroll_batch(
        self,
        *,
        account_uuid: UUID,
        query: str,
        next_batch_index: int,
        increment: int,
        max_cards: int,
    ) -> FakeJobFeedBatchV1:
        del account_uuid, query, next_batch_index, increment, max_cards
        raise ValueError("FIXTURE_HAS_NO_MORE_BATCHES")

    def open_exact_post(
        self,
        *,
        account_uuid: UUID,
        canonical_post_url: str,
    ) -> FakeJobPostDetailV1 | None:
        if account_uuid != self._batch.account_uuid:
            raise ValueError("FIXTURE_SESSION_MISMATCH")
        return self._details.get(canonical_post_url)


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class _ConsoleStopSignal:
    """Foreground-only STOP/EOF input poller; it creates no worker thread."""

    def __init__(self, input_stream: TextIO) -> None:
        self._input = input_stream
        self._stopped = False
        self._line = ""
        self._skip_extended_key = False

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, seconds: float) -> None:
        if self._stopped or seconds <= 0:
            return
        if os.name == "nt":
            self._wait_windows(seconds)
        else:
            self._wait_posix(seconds)

    def _wait_posix(self, seconds: float) -> None:
        try:
            readable, _writable, _exceptional = select.select([self._input], [], [], seconds)
            if not readable:
                return
            line = self._input.readline()
        except OSError, ValueError:
            self._stopped = True
            return
        if line == "" or line.strip().casefold() == "stop":
            self._stopped = True

    def _wait_windows(self, seconds: float) -> None:
        try:
            import msvcrt
        except ImportError:
            self._stopped = True
            return

        deadline = time.monotonic() + seconds
        while not self._stopped:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not msvcrt.kbhit():
                time.sleep(min(0.1, remaining))
                continue
            character = msvcrt.getwche()
            if self._skip_extended_key:
                self._skip_extended_key = False
            elif character in ("\x00", "\xe0"):
                self._skip_extended_key = True
            elif character == "\x1a":
                self._stopped = True
            elif character in ("\r", "\n"):
                if self._line.strip().casefold() == "stop":
                    self._stopped = True
                self._line = ""
            elif character in ("\b", "\x7f"):
                self._line = self._line[:-1]
            elif len(self._line) < 16:
                self._line += character


class _ConsoleSleeper:
    def __init__(self, stop_signal: _ConsoleStopSignal) -> None:
        self._stop_signal = stop_signal

    def sleep(self, seconds: float) -> None:
        self._stop_signal.wait(seconds)


def synthetic_account_uuid(alias: object) -> UUID:
    """Map a reserved synthetic alias without reading LocalAccountStore."""

    if not isinstance(alias, str) or _ACCOUNT_ALIAS_RE.fullmatch(alias) is None:
        raise BrowserJobsCliError("ACCOUNT_ALIAS_INVALID")
    identity = hashlib.sha256(f"{SYNTHETIC_ORIGIN}/synthetic-account/{alias}".encode()).digest()
    account_bytes = bytearray(identity[:16])
    account_bytes[6] = (account_bytes[6] & 0x0F) | 0x40
    account_bytes[8] = (account_bytes[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(account_bytes))


def run_recruitment_browser_jobs_cli(
    args: object,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    watch_factory: Callable[..., BrowserJobWatchEngine] | None = None,
    clock: WatchClock | None = None,
    sleeper: WatchSleeper | None = None,
    stop_signal: FakeStopSignal | None = None,
    input_stream: TextIO | None = None,
) -> int:
    """Run the single foreground command with redacted fixed-code output."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    source = getattr(args, "source", None)
    if source == "manual":
        if any(
            getattr(args, name, None) is not None
            for name in ("account", "data_root", "fixture_file", "run_id")
        ) or any(
            getattr(args, name, default) != default
            for name, default in (
                ("max_cycles", DEFAULT_MAX_CYCLES),
                ("max_posts_per_cycle", MAX_POSTS_PER_CYCLE),
                ("interval_minutes", MIN_INTERVAL_MINUTES),
            )
        ):
            errors.write("ERROR MANUAL_INPUT_UNSUPPORTED\n")
            return 1
        output.write("BROWSER_WATCH status=UNKNOWN source=MANUAL_URL reason=NO_TEXT\n")
        return 0
    if source != "fixture":
        errors.write("ERROR SOURCE_INVALID\n")
        return 1

    try:
        account_alias = _required_string(args, "account", "ACCOUNT_REQUIRED")
        account_uuid = synthetic_account_uuid(account_alias)
        run_id = _required_string(args, "run_id", "RUN_ID_REQUIRED")
        if _RUN_ID_RE.fullmatch(run_id) is None:
            raise BrowserJobsCliError("RUN_ID_INVALID")
        data_root = _required_path(args, "data_root", "DATA_ROOT_REQUIRED")
        fixture_path = _required_path(args, "fixture_file", "FIXTURE_REQUIRED")
        max_cycles = _integer_argument(args, "max_cycles", DEFAULT_MAX_CYCLES)
        max_posts = _integer_argument(args, "max_posts_per_cycle", MAX_POSTS_PER_CYCLE)
        interval_minutes = _integer_argument(args, "interval_minutes", MIN_INTERVAL_MINUTES)
        if (
            not 1 <= max_cycles <= MAX_CYCLES
            or not 1 <= max_posts <= MAX_POSTS_PER_CYCLE
            or interval_minutes < 0
            or interval_minutes > 35_791_394
            or (max_cycles > 1 and interval_minutes < MIN_INTERVAL_MINUTES)
        ):
            raise BrowserJobsCliError("WATCH_POLICY_INVALID")

        fixture_cycles = _load_fixture(fixture_path)
        if len(fixture_cycles) < max_cycles:
            raise BrowserJobsCliError("FIXTURE_CYCLE_COUNT_INVALID")
        if any(len(cycle.targets) > max_posts for cycle in fixture_cycles):
            raise BrowserJobsCliError("FIXTURE_POST_LIMIT_EXCEEDED")
        selected_cycles = fixture_cycles[:max_cycles]
        engine_cycles = _to_engine_cycles(selected_cycles, account_uuid)

        input_handle = input_stream or sys.stdin
        active_stop = stop_signal
        active_sleeper = sleeper
        if active_stop is None:
            if max_cycles > 1 and not _is_tty(input_handle):
                raise BrowserJobsCliError("FOREGROUND_TTY_REQUIRED")
            console_stop = _ConsoleStopSignal(input_handle)
            active_stop = console_stop
            if active_sleeper is None:
                active_sleeper = _ConsoleSleeper(console_stop)
        if active_sleeper is None:
            raise BrowserJobsCliError("WATCH_POLICY_INVALID")

        _prepare_synthetic_root(data_root)
        if max_cycles > 1 and isinstance(active_stop, _ConsoleStopSignal):
            errors.write("FOREGROUND_WAIT type STOP + Enter, EOF, or Ctrl+C to stop.\n")
        engine = (watch_factory or BrowserJobWatchEngine)(
            data_root,
            retention_policy=RetentionPolicy(raw_text_days=30, derived_vacancy_days=90),
        )
        result = engine.run(
            source=WatchSource.SYNTHETIC_FIXTURE,
            account_uuid=account_uuid,
            query=_FIXTURE_QUERY,
            run_id=run_id,
            cycles=engine_cycles,
            max_cycles=max_cycles,
            interval_seconds=interval_minutes * 60,
            clock=clock or _SystemClock(),
            sleeper=active_sleeper,
            stop_signal=cast(FakeStopSignal, active_stop),
            capture_id_factory=_capture_id_factory,
        )
    except KeyboardInterrupt:
        raise
    except BrowserJobsCliError as error:
        errors.write(f"ERROR {error.code}\n")
        return 1
    except RecruitmentJobStoreError:
        errors.write("ERROR PRIVATE_PATH_UNSAFE\n")
        return 1
    except Exception:  # noqa: BLE001 - CLI boundary is deliberately redacted.
        errors.write("ERROR BROWSER_WATCH_FAILED\n")
        return 1

    output.write(_format_result(result))
    if result.status is WatchStatus.COMPLETE:
        return 0
    if result.status is WatchStatus.STOPPED:
        return 130
    return 1


def _required_string(args: object, name: str, missing_code: str) -> str:
    value = getattr(args, name, None)
    if not isinstance(value, str) or not value:
        raise BrowserJobsCliError(missing_code)
    return value


def _required_path(args: object, name: str, missing_code: str) -> Path:
    value = _required_string(args, name, missing_code)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise BrowserJobsCliError("PATH_INVALID")
    return path


def _integer_argument(args: object, name: str, default: int) -> int:
    value = getattr(args, name, default)
    if type(value) is not int:
        raise BrowserJobsCliError("WATCH_POLICY_INVALID")
    return value


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except AttributeError, OSError, ValueError:
        return False


def _load_fixture(path: Path) -> tuple[_FixtureCycle, ...]:
    parent = path.parent
    try:
        _store_fs._validate_path_components(parent)
        _store_fs._verify_private_directory(parent)
        _store_fs._ensure_outside_checkout(parent)
        info = path.lstat()
        if info.st_size > MAX_FIXTURE_BYTES:
            raise BrowserJobsCliError("FIXTURE_TOO_LARGE")
        _store_fs._verify_regular_file(
            path,
            parent,
            allow_missing=False,
            max_bytes=MAX_FIXTURE_BYTES,
        )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > MAX_FIXTURE_BYTES
                or _store_fs._is_reparse_point(opened)
            ):
                raise BrowserJobsCliError("FIXTURE_UNSAFE")
            raw = stream.read(MAX_FIXTURE_BYTES + 1)
        if len(raw) > MAX_FIXTURE_BYTES:
            raise BrowserJobsCliError("FIXTURE_TOO_LARGE")
        _store_fs._verify_regular_file(
            path,
            parent,
            allow_missing=False,
            max_bytes=MAX_FIXTURE_BYTES,
        )
    except BrowserJobsCliError:
        raise
    except OSError, RuntimeError, ValueError, RecruitmentJobStoreError:
        raise BrowserJobsCliError("FIXTURE_UNSAFE") from None

    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
        raise BrowserJobsCliError("FIXTURE_INVALID") from None
    return _decode_fixture_document(document)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("INVALID_JSON_CONSTANT")


def _decode_fixture_document(value: object) -> tuple[_FixtureCycle, ...]:
    document = _exact_object(value, {"schema_version", "fixture_kind", "cycles"})
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["fixture_kind"] != "SYNTHETIC_ONLY"
    ):
        raise BrowserJobsCliError("FIXTURE_INVALID")
    raw_cycles = document["cycles"]
    if type(raw_cycles) is not list:
        raise BrowserJobsCliError("FIXTURE_CYCLE_COUNT_INVALID")
    raw_cycles = cast(list[object], raw_cycles)
    if not 1 <= len(raw_cycles) <= MAX_CYCLES:
        raise BrowserJobsCliError("FIXTURE_CYCLE_COUNT_INVALID")

    cycles: list[_FixtureCycle] = []
    for raw_cycle in raw_cycles:
        cycle = _exact_object(raw_cycle, {"targets", "posts"})
        raw_targets = cycle["targets"]
        raw_posts = cycle["posts"]
        if type(raw_targets) is not list or type(raw_posts) is not list:
            raise BrowserJobsCliError("FIXTURE_TARGET_PLAN_INVALID")
        raw_targets = cast(list[object], raw_targets)
        raw_posts = cast(list[object], raw_posts)
        if not 1 <= len(raw_targets) <= MAX_POSTS_PER_CYCLE or len(raw_posts) != len(raw_targets):
            raise BrowserJobsCliError("FIXTURE_TARGET_PLAN_INVALID")
        targets: list[str] = []
        posts: list[_FixturePost] = []
        for raw_target, raw_post in zip(raw_targets, raw_posts, strict=True):
            if not isinstance(raw_target, str) or not _is_reserved_post_url(raw_target):
                raise BrowserJobsCliError("FIXTURE_TARGET_INVALID")
            if raw_target in targets:
                raise BrowserJobsCliError("FIXTURE_TARGET_PLAN_INVALID")
            post = _decode_fixture_post(raw_post)
            if post.url != raw_target:
                raise BrowserJobsCliError("FIXTURE_TARGET_PLAN_INVALID")
            targets.append(raw_target)
            posts.append(post)
        cycles.append(_FixtureCycle(tuple(targets), tuple(posts)))
    return tuple(cycles)


def _exact_object(value: object, expected_keys: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise BrowserJobsCliError("FIXTURE_INVALID")
    result = cast(dict[object, object], value)
    if any(type(key) is not str for key in result) or set(result) != expected_keys:
        raise BrowserJobsCliError("FIXTURE_INVALID")
    return cast(dict[str, object], result)


def _decode_fixture_post(value: object) -> _FixturePost:
    post = _exact_object(
        value,
        {"url", "body", "body_visibility", "published_at_utc"},
    )
    url = post["url"]
    body = post["body"]
    visibility_value = post["body_visibility"]
    published_value = post["published_at_utc"]
    if not isinstance(url, str) or not _is_reserved_post_url(url):
        raise BrowserJobsCliError("FIXTURE_TARGET_INVALID")
    if body is not None and (
        type(body) is not str
        or len(body) > MAX_POST_BODY_CODEPOINTS
        or _contains_contact_or_remote_url(body)
    ):
        raise BrowserJobsCliError("FIXTURE_BODY_INVALID")
    if isinstance(body, str):
        try:
            body.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise BrowserJobsCliError("FIXTURE_BODY_INVALID") from None
    try:
        visibility = FakeBodyVisibility(visibility_value)
    except TypeError, ValueError:
        raise BrowserJobsCliError("FIXTURE_BODY_INVALID") from None
    if body is None and visibility is not FakeBodyVisibility.UNKNOWN:
        raise BrowserJobsCliError("FIXTURE_BODY_INVALID")
    if body == "" and visibility is FakeBodyVisibility.FULL_VISIBLE:
        raise BrowserJobsCliError("FIXTURE_BODY_INVALID")
    if published_value is None:
        published = None
    elif isinstance(published_value, str):
        published = _parse_utc_timestamp(published_value)
    else:
        raise BrowserJobsCliError("FIXTURE_TIMESTAMP_INVALID")
    return _FixturePost(url, body, visibility, published)


def _is_reserved_post_url(value: str) -> bool:
    if _POST_URL_RE.fullmatch(value) is None:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "threads.example.invalid"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
    )


def _contains_contact_or_remote_url(value: str) -> bool:
    without_placeholders = _SAFE_SYNTHETIC_CONTACT_RE.sub("", value)
    return bool(
        _EMAIL_RE.search(without_placeholders)
        or _PHONE_RE.search(without_placeholders)
        or _URL_RE.search(without_placeholders)
    )


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except OverflowError, ValueError:
        raise BrowserJobsCliError("FIXTURE_TIMESTAMP_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise BrowserJobsCliError("FIXTURE_TIMESTAMP_INVALID")
    return parsed.astimezone(UTC)


def _to_engine_cycles(
    fixture_cycles: Sequence[_FixtureCycle],
    account_uuid: UUID,
) -> tuple[SyntheticCycleInput, ...]:
    cycles: list[SyntheticCycleInput] = []
    for cycle_index, cycle in enumerate(fixture_cycles, start=1):
        cards: list[FakeJobCardV1] = []
        details: dict[str, FakeJobPostDetailV1] = {}
        for post_index, post in enumerate(cycle.posts, start=1):
            card_id = f"fixture-{cycle_index:02d}-{post_index:03d}"
            cards.append(
                FakeJobCardV1(
                    account_uuid=account_uuid,
                    local_card_id=card_id,
                    canonical_post_url=post.url,
                    author_marker="synthetic-employer",
                )
            )
            details[post.url] = FakeJobPostDetailV1(
                requested_url=post.url,
                page_url=post.url,
                canonical_post_url=post.url,
                account_uuid=account_uuid,
                state=FakePageState.READY,
                author_marker="synthetic-employer",
                body=post.body,
                body_visibility=post.visibility,
                post_kind=ObservedPostKind.ORIGINAL,
                published_at_utc=post.published_at_utc,
            )
        batch = FakeJobFeedBatchV1(
            batch_index=0,
            account_uuid=account_uuid,
            query=_FIXTURE_QUERY,
            page_url=SYNTHETIC_SEARCH_URL,
            state=FakePageState.READY,
            cards=tuple(cards),
            has_more=False,
        )
        session = _FixtureSession(batch, details)
        cycles.append(SyntheticCycleInput(session=session, selected_targets=cycle.targets))
    return tuple(cycles)


def _capture_id_factory(
    *,
    account_uuid: UUID,
    run_id: str,
    generation: int,
    cycle_number: int,
    observed_at_utc: datetime,
) -> str:
    identity = (
        f"{account_uuid}|{run_id}|{generation}|{cycle_number}|"
        f"{observed_at_utc.astimezone(UTC).isoformat(timespec='microseconds')}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"cap-{digest}"


def _prepare_synthetic_root(root: Path) -> None:
    if not root.is_absolute() or ".." in root.parts:
        raise BrowserJobsCliError("DATA_ROOT_INVALID")
    parent = root.parent
    marker = root / _FIXTURE_ROOT_MARKER
    try:
        _store_fs._validate_path_components(parent)
        _store_fs._verify_private_directory(parent)
        _store_fs._ensure_outside_checkout(parent)
        try:
            root.lstat()
        except FileNotFoundError:
            raise BrowserJobsCliError("DATA_ROOT_REQUIRED") from None
        _store_fs._verify_private_directory(root)
        _store_fs._ensure_outside_checkout(root)
        names = {entry.name for entry in root.iterdir()}
        if not names.issubset(_ALLOWED_ROOT_ENTRIES):
            raise BrowserJobsCliError("DATA_ROOT_NOT_SYNTHETIC")
        if _FIXTURE_ROOT_MARKER not in names:
            if names:
                raise BrowserJobsCliError("DATA_ROOT_NOT_SYNTHETIC")
            _create_root_marker(marker, root)
            names.add(_FIXTURE_ROOT_MARKER)
        _store_fs._verify_regular_file(
            marker,
            root,
            allow_missing=False,
            max_bytes=len(_FIXTURE_ROOT_MARKER_BYTES),
        )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(marker, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _store_fs._is_reparse_point(opened):
                raise BrowserJobsCliError("DATA_ROOT_UNSAFE")
            contents = stream.read(len(_FIXTURE_ROOT_MARKER_BYTES) + 1)
        if contents != _FIXTURE_ROOT_MARKER_BYTES:
            raise BrowserJobsCliError("DATA_ROOT_NOT_SYNTHETIC")
    except BrowserJobsCliError:
        raise
    except FileExistsError:
        raise BrowserJobsCliError("DATA_ROOT_CONFLICT") from None
    except OSError, RuntimeError, ValueError, RecruitmentJobStoreError:
        raise BrowserJobsCliError("DATA_ROOT_UNSAFE") from None


def _create_root_marker(marker: Path, root: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(_FIXTURE_ROOT_MARKER_BYTES)
            stream.flush()
            os.fsync(stream.fileno())
        _store_fs._secure_file(marker)
        _store_fs._verify_regular_file(
            marker,
            root,
            allow_missing=False,
            max_bytes=len(_FIXTURE_ROOT_MARKER_BYTES),
        )
    except FileExistsError:
        raise BrowserJobsCliError("DATA_ROOT_CONFLICT") from None
    except OSError, RecruitmentJobStoreError:
        raise BrowserJobsCliError("DATA_ROOT_UNSAFE") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _format_result(result: WatchRunResult) -> str:
    status = result.status.value
    reason_value = result.reason_code
    reason = "-"
    if reason_value is not None:
        candidate = getattr(reason_value, "value", reason_value)
        allowed_reasons = {item.value for item in WatchReasonCode}
        if isinstance(candidate, str) and candidate in allowed_reasons:
            reason = candidate
        else:
            reason = "UNCLASSIFIED"
    next_due = "-"
    if isinstance(result.next_due_utc, datetime):
        try:
            next_due = result.next_due_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except OverflowError, ValueError:
            next_due = "-"
    return (
        f"BROWSER_WATCH status={status} source=SYNTHETIC_FIXTURE reason={reason} "
        f"scanned={_safe_count(result.scanned)} vacancy={_safe_count(result.vacancy)} "
        f"duplicate={_safe_count(result.duplicate)} partial={_safe_count(result.partial)} "
        f"errors={_safe_count(result.errors)} completed_cycles={_safe_count(result.completed_cycles)} "
        f"next_due_utc={next_due}\n"
    )


def _safe_count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0
