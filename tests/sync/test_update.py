"""Acceptance tests for ``garmin-archive update`` (spec-81da0774, criteria 2-13).

Everything runs against ``FakeGarmin`` on ``httpx.MockTransport`` with an initialized
database in ``tmp_path``. Library tests inject a clock; CLI tests inject the fake HTTP
client and a no-op ``sleep`` so retries never wait.
"""

from __future__ import annotations

import functools
import json
import logging
import signal
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_archive import cli as cli_module
from garmin_archive.api import DEFAULT_MAX_ATTEMPTS, GarminApiClient
from garmin_archive.archive import (
    KIND_ORIGINAL_FILE,
    KIND_SEARCH_PAGE,
    KIND_SUMMARY,
    KIND_WORKOUTS,
    ArchiveReader,
    ArchiveWriter,
)
from garmin_archive.cli import main
from garmin_archive.client import ArchiveClient
from garmin_archive.config import ArchiveConfig
from garmin_archive.models import (
    AccountMismatchError,
    RateLimitError,
)
from garmin_archive.storage.connection import open_write_connection
from garmin_archive.storage.lock import writer_lock
from garmin_archive.storage.metadata import bind_account, get_account_binding, get_metadata
from garmin_archive.sync.discovery import PAGE_SIZE_KEY
from garmin_archive.sync.update import UpdateResult, run_update
from sync.fake_garmin import (
    FAKE_PROFILE_ID,
    MULTISPORT_TYPE_KEY,
    UNKNOWN_TYPE_KEY,
    FakeGarmin,
)

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
KINDS = (KIND_SUMMARY, KIND_WORKOUTS, KIND_ORIGINAL_FILE)


class StepClock:
    """Injectable clock whose time is set by the test."""

    def __init__(self, now: datetime = T0) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def _api(fake: FakeGarmin) -> GarminApiClient:
    return GarminApiClient(fake.api_key, http_client=fake.client(), sleep=lambda _s: None)


@dataclass
class Archive:
    path: Path
    clock: StepClock

    def update(
        self,
        fake: FakeGarmin,
        *,
        full: bool = False,
        stop_flag: Callable[[], bool] = lambda: False,
    ) -> UpdateResult:
        with ArchiveClient(ArchiveConfig(db_path=self.path), clock=self.clock) as client:
            return client.update(_api(fake), full=full, stop_flag=stop_flag)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def scalar(self, sql: str, *params: object) -> Any:
        with closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        with closing(self.connect()) as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]

    def payload_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM raw_payloads"))

    def states(self, activity_id: str) -> dict[str, str]:
        with closing(self.connect()) as conn:
            return ArchiveReader(conn).resource_states(activity_id)

    def body(self, activity_id: str, kind: str) -> bytes:
        with closing(self.connect()) as conn:
            payload = ArchiveReader(conn).current_payload(activity_id, kind)
        assert payload is not None
        return payload.body_blob

    def last_run(self) -> tuple[Any, ...]:
        return self.rows(
            "SELECT status, counters_json, coverage_json, parameters_json, mode"
            " FROM sync_runs ORDER BY id DESC LIMIT 1"
        )[0]


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    path = tmp_path / "archive.db"
    clock = StepClock()
    with ArchiveClient(ArchiveConfig(db_path=path), clock=clock) as client:
        client.initialize()
    return Archive(path, clock)


@dataclass
class CliRun:
    code: int
    out: str
    err: str

    @property
    def doc(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.out)
        return loaded


@pytest.fixture
def cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., CliRun]:
    monkeypatch.setattr(
        cli_module, "GarminApiClient", functools.partial(GarminApiClient, sleep=lambda _s: None)
    )

    def run(
        archive: Archive, fake: FakeGarmin, *args: str, key: bool = True, db: bool = True
    ) -> CliRun:
        if key:
            monkeypatch.setenv("GARMIN_API_KEY", fake.api_key)
        else:
            monkeypatch.delenv("GARMIN_API_KEY", raising=False)
        argv = ["--db", str(archive.path), "update", *args] if db else ["update", *args]
        code = main(argv, http_client=fake.client())
        captured = capsys.readouterr()
        return CliRun(code, captured.out, captured.err)

    return run


def _assert_no_secrets(fake: FakeGarmin, archive: Archive, *texts: str) -> None:
    db_bytes = b"".join(
        p.read_bytes() for p in archive.path.parent.glob(archive.path.name + "*") if p.is_file()
    )
    for secret in fake.secrets:
        for text in texts:
            assert secret not in text
        assert secret.encode() not in db_bytes


# ---- 2. first run ----------------------------------------------------------------------


def test_first_run_archives_everything(
    archive: Archive,
    cli: Callable[..., CliRun],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeGarmin(250)
    assert fake.unknown_id is not None
    assert fake.parent_id is not None
    assert len(fake.child_ids) == 2

    result = cli(archive, fake)
    assert result.code == 0, result.out
    data = result.doc["data"]
    assert data["status"] == "complete"
    assert data["inventory"] == "complete"
    assert data["hint"] is None
    assert data["counters"]["discovered"] == 250
    assert data["counters"]["new"] == 250
    assert data["counters"]["downloaded"] == 750
    assert data["counters"]["failed"] == 0
    for kind in KINDS:
        assert data["coverage"][kind]["available"] == 250

    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 250
    for aid in fake.activity_ids:
        assert archive.body(aid, KIND_SUMMARY) == fake.summaries[aid]
        assert archive.body(aid, KIND_WORKOUTS) == fake.workouts[aid]
        assert archive.body(aid, KIND_ORIGINAL_FILE) == fake.files[aid]
        assert {archive.states(aid)[k] for k in KINDS} == {"available"}

    def type_key(aid: str) -> str | None:
        return archive.scalar("SELECT type_key FROM activity_archive WHERE activity_id = ?", aid)

    assert type_key(fake.unknown_id) == UNKNOWN_TYPE_KEY
    assert type_key(fake.parent_id) == MULTISPORT_TYPE_KEY
    for child in fake.child_ids:
        assert (
            archive.scalar(
                "SELECT parent_activity_id FROM activity_archive WHERE activity_id = ?", child
            )
            == fake.parent_id
        )
    with closing(archive.connect()) as conn:
        assert get_account_binding(conn) == FAKE_PROFILE_ID
    status, counters_json, coverage_json, _params, mode = archive.last_run()
    assert (status, mode) == ("complete", "incremental")
    assert json.loads(counters_json)["downloaded"] == 750
    assert json.loads(coverage_json)["inventory"] == "complete"
    _assert_no_secrets(fake, archive, result.out, result.err, caplog.text)


# ---- 3. second unchanged run -----------------------------------------------------------


def test_second_unchanged_run_fetches_nothing(archive: Archive) -> None:
    fake = FakeGarmin(60)
    assert archive.update(fake).status == "complete"
    payloads = archive.payload_count()
    fake.reset_counts()
    archive.clock.current = T0 + timedelta(days=30)
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.counters.downloaded == 0
    assert result.counters.new == 0
    assert result.counters.discovered == 60
    for kind in ("summary", "workouts", "file"):
        assert fake.count(kind) == 0
    assert archive.payload_count() == payloads


# ---- 4. change detection ---------------------------------------------------------------


def test_changed_search_item_refetches_only_that_activity(archive: Archive) -> None:
    fake = FakeGarmin(30)
    archive.update(fake)
    changed = fake.activity_ids[7]
    fake.mutate_search_item(changed, syntheticChange=1)
    fake.reset_counts()
    archive.clock.current = T0 + timedelta(days=30)
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.counters.changed == 1
    assert result.counters.new == 0
    assert result.counters.unchanged == 3
    for aid in fake.activity_ids:
        expected = 1 if aid == changed else 0
        for kind in ("summary", "workouts", "file"):
            assert fake.count_for(kind, aid) == expected


def test_recent_activities_are_refetched_and_deduplicated(archive: Archive) -> None:
    fake = FakeGarmin(60)
    archive.update(fake)
    payloads_before = archive.payload_count()
    fake.reset_counts()
    last = fake.start_times[fake.activity_ids[-1]]
    archive.clock.current = last + timedelta(days=1)
    threshold = archive.clock.current - timedelta(days=14)
    recent = {a for a in fake.activity_ids if fake.start_times[a] >= threshold}
    assert 0 < len(recent) < 60
    result = archive.update(fake)
    assert result.status == "complete"
    for aid in fake.activity_ids:
        assert fake.count_for("summary", aid) == (1 if aid in recent else 0)
        assert fake.count_for("workouts", aid) == 0
        assert fake.count_for("file", aid) == 0
    assert result.counters.downloaded == 0
    assert result.counters.unchanged == len(recent)
    assert archive.payload_count() == payloads_before


def test_changed_item_inside_recent_window_refetches_all_three(archive: Archive) -> None:
    fake = FakeGarmin(60)
    archive.update(fake)
    changed = fake.activity_ids[-1]
    fake.mutate_search_item(changed, syntheticChange=1)
    fake.reset_counts()
    archive.clock.current = fake.start_times[changed] + timedelta(days=1)
    archive.update(fake)
    for kind in ("summary", "workouts", "file"):
        assert fake.count_for(kind, changed) == 1


def test_unchanged_sync_against_regenerating_api_adds_no_payloads(archive: Archive) -> None:
    fake = FakeGarmin(60)
    fake.regenerate_bodies = True
    archive.update(fake)
    payloads = archive.payload_count()
    fake.reset_counts()
    archive.clock.current = fake.start_times[fake.activity_ids[-1]] + timedelta(days=1)
    result = archive.update(fake, full=True)
    assert fake.count("file") == 60
    assert result.counters.downloaded == 0
    assert archive.payload_count() == payloads


# ---- 5. page-size fallback -------------------------------------------------------------


def test_page_size_falls_back_and_is_stored(archive: Archive) -> None:
    fake = FakeGarmin(100)
    fake.fail_above_limit = 30
    result = archive.update(fake)
    assert result.status == "complete"
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 100
    # The 49 request is retried by the client; the fallback step uses a single attempt.
    assert fake.search_limits[: DEFAULT_MAX_ATTEMPTS + 1] == [49] * DEFAULT_MAX_ATTEMPTS + [24]
    assert set(fake.search_limits[DEFAULT_MAX_ATTEMPTS:]) == {24}
    with closing(archive.connect()) as conn:
        assert get_metadata(conn, PAGE_SIZE_KEY) == 24

    fake.reset_counts()
    archive.clock.current = T0 + timedelta(days=30)
    assert archive.update(fake).status == "complete"
    assert set(fake.search_limits) == {24}


def test_page_size_halves_repeatedly_to_the_working_size(archive: Archive) -> None:
    fake = FakeGarmin(40)
    fake.fail_above_limit = 12
    assert archive.update(fake).status == "complete"
    assert fake.search_limits[: DEFAULT_MAX_ATTEMPTS + 2] == [49] * DEFAULT_MAX_ATTEMPTS + [24, 12]
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 40


def test_server_page_cap_below_requested_limit(archive: Archive) -> None:
    fake = FakeGarmin(100)
    fake.page_cap = 40
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.inventory == "complete"
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 100
    assert result.counters.discovered == 100


# ---- 6. unavailable resources ----------------------------------------------------------


def test_file_404_is_unavailable_and_not_retried_until_full(archive: Archive) -> None:
    fake = FakeGarmin(20)
    target = fake.activity_ids[3]
    fake.fail("file", target, status=404)
    first = archive.update(fake)
    assert first.status == "complete"
    assert first.counters.unavailable == 1
    assert archive.states(target)[KIND_ORIGINAL_FILE] == "unavailable"

    fake.reset_counts()
    archive.clock.current = T0 + timedelta(days=30)
    assert archive.update(fake).status == "complete"
    assert fake.count_for("file", target) == 0

    full = archive.update(fake, full=True)
    assert full.status == "complete"
    assert fake.count_for("file", target) == 1
    assert full.counters.unavailable == 1
    assert archive.last_run()[4] == "full"


def test_summary_404_marks_not_found_and_deletes_nothing(archive: Archive) -> None:
    fake = FakeGarmin(20)
    target = fake.activity_ids[5]
    archive.update(fake)
    payloads = archive.payload_count()
    summary_body = archive.body(target, KIND_SUMMARY)

    fake.fail("summary", target, status=404)
    result = archive.update(fake, full=True)
    assert result.status == "complete"
    assert archive.scalar(
        "SELECT availability_status FROM activity_archive WHERE activity_id = ?", target
    ) == ("not_found")
    assert archive.states(target)[KIND_SUMMARY] == "unavailable"
    assert archive.payload_count() >= payloads
    assert archive.body(target, KIND_SUMMARY) == summary_body
    assert archive.body(target, KIND_WORKOUTS) == fake.workouts[target]
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 20


def test_forbidden_resource_is_a_known_outcome(archive: Archive) -> None:
    fake = FakeGarmin(10)
    target = fake.activity_ids[2]
    fake.fail("workouts", target, status=403)
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.counters.forbidden == 1
    assert archive.states(target)[KIND_WORKOUTS] == "forbidden"


# ---- 7. partial and retry --------------------------------------------------------------


def test_persistent_503_is_partial_exit_6_then_retried(
    archive: Archive, cli: Callable[..., CliRun]
) -> None:
    fake = FakeGarmin(20)
    target = fake.activity_ids[4]
    fake.fail("summary", target, status=503)
    first = cli(archive, fake)
    assert first.code == 6
    assert first.doc["status"] == "partial"
    data = first.doc["data"]
    assert data["status"] == "partial"
    assert data["counters"]["failed"] == 1
    assert data["hint"]
    assert archive.states(target)[KIND_SUMMARY] == "failed"
    assert archive.last_run()[0] == "partial"

    fake.clear_failures()
    fake.reset_counts()
    second = cli(archive, fake)
    assert second.code == 0
    assert second.doc["status"] == "ok"
    assert second.doc["data"]["status"] == "complete"
    assert second.doc["data"]["hint"] is None
    assert fake.count_for("summary", target) == 1
    assert fake.count("summary") == 1
    assert archive.states(target)[KIND_SUMMARY] == "available"


def test_rate_limit_mid_fetch_is_partial_then_completes(
    archive: Archive, cli: Callable[..., CliRun]
) -> None:
    fake = FakeGarmin(20)
    # 2 search requests (page + empty page), then 9 fetches succeed before the 12th request.
    fake.rate_limit(at_request=12)
    first = cli(archive, fake)
    assert first.code == 6
    assert first.doc["data"]["status"] == "partial"
    assert first.doc["data"]["counters"]["downloaded"] == 9
    assert first.doc["data"]["pending"] == 51
    assert first.doc["data"]["inventory"] == "complete"

    fake.clear_rate_limit()
    second = cli(archive, fake)
    assert second.code == 0
    assert second.doc["data"]["counters"]["downloaded"] == 51
    for aid in fake.activity_ids:
        assert {archive.states(aid)[k] for k in KINDS} == {"available"}


def test_rate_limit_on_first_page_fails_with_exit_4(
    archive: Archive, cli: Callable[..., CliRun]
) -> None:
    fake = FakeGarmin(20)
    fake.rate_limit(at_request=1)
    result = cli(archive, fake)
    assert result.code == 4
    assert result.doc["status"] == "error"
    assert result.doc["error"]["code"] == "RATE_LIMITED"
    assert archive.last_run()[0] == "failed"
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 0


def test_rate_limit_on_first_page_raises_in_library(archive: Archive) -> None:
    fake = FakeGarmin(20)
    fake.rate_limit(at_request=1)
    with pytest.raises(RateLimitError):
        archive.update(fake)
    assert archive.last_run()[0] == "failed"


def test_search_failure_with_pending_work_still_fetches(archive: Archive) -> None:
    fake = FakeGarmin(100)
    archive.update(fake, stop_flag=lambda: fake.count("search") >= 1)
    fake.reset_counts()
    fake.fail("search", status=503)
    archive.clock.current = T0 + timedelta(days=30)
    result = archive.update(fake)
    assert result.status == "partial"
    assert result.inventory == "incomplete"
    assert result.incomplete_reason == "page_failed"
    assert fake.count("summary") > 0


# ---- 8. interrupted discovery ----------------------------------------------------------


def test_interrupted_discovery_then_each_activity_fetched_once(archive: Archive) -> None:
    fake = FakeGarmin(120)
    first = archive.update(fake, stop_flag=lambda: fake.count("search") >= 2)
    assert first.status == "interrupted"
    assert first.inventory == "incomplete"
    assert first.incomplete_reason == "interrupted"
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 98
    assert first.pending == 98 * 3
    assert first.hint
    assert archive.last_run()[0] == "interrupted"

    second = archive.update(fake)
    assert second.status == "complete"
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 120
    for aid in fake.activity_ids:
        for kind in ("summary", "workouts", "file"):
            assert fake.count_for(kind, aid) == 1


def test_sigint_exits_130_restores_handler_and_next_run_continues(
    archive: Archive, cli: Callable[..., CliRun]
) -> None:
    fake = FakeGarmin(20)
    raised: list[bool] = []

    def interrupt(kind: str, _aid: str | None, _request: object) -> None:
        if kind == "summary" and not raised:
            raised.append(True)
            signal.raise_signal(signal.SIGINT)

    fake.on_request = interrupt
    before = signal.getsignal(signal.SIGINT)
    first = cli(archive, fake)
    assert first.code == 130
    assert first.doc["status"] == "interrupted"
    assert first.doc["data"]["status"] == "interrupted"
    assert signal.getsignal(signal.SIGINT) is before
    assert archive.last_run()[0] == "interrupted"

    fake.on_request = None
    second = cli(archive, fake)
    assert second.code == 0
    for aid in fake.activity_ids:
        for kind in ("summary", "workouts", "file"):
            assert fake.count_for(kind, aid) == 1


def test_stale_running_row_is_marked_interrupted(archive: Archive) -> None:
    with closing(archive.connect()) as conn:
        conn.execute(
            "INSERT INTO sync_runs (mode, started_at_utc, status) VALUES"
            " ('incremental', '2024-01-01T00:00:00.000000Z', 'running')"
        )
        conn.commit()
    assert archive.update(FakeGarmin(5)).status == "complete"
    statuses = [r[0] for r in archive.rows("SELECT status FROM sync_runs ORDER BY id")]
    assert statuses == ["interrupted", "complete"]


# ---- 9. account mismatch ---------------------------------------------------------------


def test_summary_for_another_profile_exits_3_and_keeps_payload(
    archive: Archive, cli: Callable[..., CliRun]
) -> None:
    fake = FakeGarmin(10)
    offender = fake.activity_ids[-1]
    fake.mismatch_profile([offender])
    result = cli(archive, fake)
    assert result.code == 3
    assert result.doc["error"]["code"] == AccountMismatchError.code
    assert archive.body(offender, KIND_SUMMARY) == fake.summaries[offender]
    assert archive.last_run()[0] == "failed"
    with closing(archive.connect()) as conn:
        assert get_account_binding(conn) == FAKE_PROFILE_ID


def test_search_page_for_another_owner_is_rejected_before_writing(archive: Archive) -> None:
    fake = FakeGarmin(10)
    with closing(archive.connect()) as conn:
        bind_account(conn, FAKE_PROFILE_ID, archive.clock)
    fake.mismatch_owner([fake.activity_ids[4]])
    with pytest.raises(AccountMismatchError):
        archive.update(fake)
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 0
    assert archive.scalar("SELECT COUNT(*) FROM raw_payloads WHERE kind = ?", KIND_SEARCH_PAGE) == 0
    assert archive.last_run()[0] == "failed"


def test_missing_api_key_exits_3(archive: Archive, cli: Callable[..., CliRun]) -> None:
    result = cli(archive, FakeGarmin(3), key=False)
    assert result.code == 3
    assert result.doc["status"] == "error"


def test_rejected_api_key_exits_3(archive: Archive, cli: Callable[..., CliRun]) -> None:
    fake = FakeGarmin(3)
    fake.reject_auth = True
    result = cli(archive, fake)
    assert result.code == 3
    assert archive.last_run()[0] == "failed"


# ---- 10. atomic pages ------------------------------------------------------------------


def test_failure_midway_through_a_page_leaves_nothing_from_it(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGarmin(20)
    original = ArchiveWriter.record_search_item
    calls: list[int] = []

    def flaky(self: ArchiveWriter, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 5:
            raise RuntimeError("injected failure")
        original(self, **kwargs)

    monkeypatch.setattr(ArchiveWriter, "record_search_item", flaky)
    with pytest.raises(RuntimeError, match="injected failure"):
        archive.update(fake)
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 0
    assert archive.scalar("SELECT COUNT(*) FROM activity_resources") == 0
    assert archive.scalar("SELECT COUNT(*) FROM raw_payloads") == 0
    assert archive.scalar("SELECT COUNT(*) FROM fetch_events") == 0
    status, counters_json, *_ = archive.last_run()
    assert status == "failed"
    assert json.loads(counters_json)["discovered"] == 0


def test_earlier_pages_survive_a_failure_on_a_later_page(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGarmin(120)
    original = ArchiveWriter.record_search_item
    calls: list[int] = []

    def flaky(self: ArchiveWriter, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 49 + 3:
            raise RuntimeError("injected failure")
        original(self, **kwargs)

    monkeypatch.setattr(ArchiveWriter, "record_search_item", flaky)
    with pytest.raises(RuntimeError):
        archive.update(fake)
    assert archive.scalar("SELECT COUNT(*) FROM activity_archive") == 49
    assert archive.scalar("SELECT COUNT(*) FROM raw_payloads WHERE kind = ?", KIND_SEARCH_PAGE) == 1
    assert json.loads(archive.last_run()[1])["discovered"] == 49


# ---- 11. no transaction during HTTP ----------------------------------------------------


def test_no_transaction_open_during_any_request(archive: Archive) -> None:
    fake = FakeGarmin(60)
    fake.fail("summary", fake.activity_ids[1], status=404)
    fake.fail("workouts", fake.activity_ids[2], status=503, times=1)
    conn = open_write_connection(archive.path, busy_timeout_ms=5000)
    try:
        fake.watch_connection(conn)
        result = run_update(_api(fake), conn, archive.clock)
    finally:
        conn.close()
    assert result.status == "complete"
    assert fake.transaction_violations == []
    assert fake.data_request_count > 180


def test_watch_connection_detects_an_open_transaction(archive: Archive) -> None:
    fake = FakeGarmin(3)
    conn = open_write_connection(archive.path, busy_timeout_ms=5000)
    try:
        fake.watch_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(AssertionError):
            _api(fake).search_activities(0, 10)
        conn.rollback()
    finally:
        conn.close()
    assert fake.transaction_violations


# ---- 12. arguments and preconditions ---------------------------------------------------


def test_unknown_option_exits_2(archive: Archive, cli: Callable[..., CliRun]) -> None:
    result = cli(archive, FakeGarmin(3), "--bogus")
    assert result.code == 2
    assert result.doc["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_database_exits_5_and_creates_no_file(
    tmp_path: Path, cli: Callable[..., CliRun]
) -> None:
    missing = Archive(tmp_path / "nope" / "missing.db", StepClock())
    result = cli(missing, FakeGarmin(3))
    assert result.code == 5
    assert result.doc["error"]["code"] == "DATABASE_NOT_FOUND"
    assert not missing.path.exists()
    assert not missing.path.with_name(missing.path.name + ".lock").exists()
    assert not missing.path.parent.exists()


def test_uninitialized_database_exits_5(tmp_path: Path, cli: Callable[..., CliRun]) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    result = cli(Archive(path, StepClock()), FakeGarmin(3))
    assert result.code == 5
    assert result.doc["error"]["code"] == "NOT_INITIALIZED"


def test_held_writer_lock_exits_5(archive: Archive, cli: Callable[..., CliRun]) -> None:
    fake = FakeGarmin(3)
    with writer_lock(archive.path.resolve()):
        result = cli(archive, fake)
    assert result.code == 5
    assert result.doc["error"]["code"] == "ARCHIVE_LOCKED"
    assert fake.count("search") == 0


def test_full_flag_reaches_the_library(archive: Archive, cli: Callable[..., CliRun]) -> None:
    fake = FakeGarmin(5)
    assert cli(archive, fake).code == 0
    fake.reset_counts()
    assert cli(archive, fake).code == 0
    assert fake.count("summary") == 0
    assert cli(archive, fake, "--full").code == 0
    assert fake.count("summary") == 5
    assert json.loads(archive.last_run()[3]) == {"full": True}


# ---- 13. output hygiene ----------------------------------------------------------------


def test_stdout_is_one_json_document_and_secrets_never_leak(
    archive: Archive,
    cli: Callable[..., CliRun],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeGarmin(12)
    fake.fail("summary", fake.activity_ids[0], status=503)
    results = [cli(archive, fake), cli(archive, FakeGarmin(3), key=False)]
    fake.reject_auth = True
    results.append(cli(archive, fake, "--full"))
    for result in results:
        assert result.out.endswith("\n")
        assert result.out.count("\n") == 1
        json.loads(result.out)
        _assert_no_secrets(fake, archive, result.out, result.err, caplog.text)


@pytest.fixture(autouse=True)
def _no_stray_handlers() -> Iterator[None]:
    before = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, before)


# ---- activity processing at the end of update (spec-939594b5, criterion 9) --------------


def test_update_processes_fetched_activities_and_reports_counters(archive: Archive) -> None:
    fake = FakeGarmin(12)
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.processed == 12
    assert result.processing_failed == 0
    data = result.to_dict()
    assert data["processed"] == 12
    assert data["processing_failed"] == 0
    assert archive.scalar("SELECT COUNT(*) FROM activities") == 12
    assert archive.scalar("SELECT COUNT(*) FROM processing_runs") == 12
    assert archive.scalar("SELECT COUNT(DISTINCT sport_family) FROM activities") >= 3

    # Nothing changed upstream: the second update processes nothing.
    archive.clock.current = T0 + timedelta(days=1)
    again = archive.update(fake)
    assert again.processed == 0
    assert again.processing_failed == 0
    assert archive.scalar("SELECT COUNT(*) FROM processing_runs") == 12


def test_update_cli_output_has_processing_counters(
    archive: Archive, cli: Callable[..., CliRun]
) -> None:
    result = cli(archive, FakeGarmin(5))
    assert result.code == 0
    assert result.doc["data"]["processed"] == 5
    assert result.doc["data"]["processing_failed"] == 0


def test_update_counts_partial_activities_as_processed(archive: Archive) -> None:
    fake = FakeGarmin(6)
    broken = fake.activity_ids[3]
    fake.files[broken] = b"not a zip"
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.processed == 6
    assert result.processing_failed == 0
    assert (
        archive.scalar("SELECT processing_status FROM activities WHERE activity_id = ?", broken)
        == "partial"
    )


def test_processing_failure_leaves_sync_status_complete(archive: Archive) -> None:
    fake = FakeGarmin(6)
    with closing(archive.connect()) as conn:
        conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON activities"
            f" WHEN NEW.activity_id = '{fake.activity_ids[2]}'"
            " BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        conn.commit()
    result = archive.update(fake)
    assert result.status == "complete"
    assert result.processing_failed == 1
    assert result.processed == 5
    assert archive.last_run()[0] == "complete"


def test_processing_exception_leaves_sync_status_complete(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    from garmin_archive.models import StorageError
    from garmin_archive.sync import update as update_module

    def boom(*_args: object, **_kwargs: object) -> None:
        raise StorageError("processing exploded")

    monkeypatch.setattr(update_module, "process_pending", boom)
    result = archive.update(FakeGarmin(4))
    assert result.status == "complete"
    assert result.processing_failed > 0
    assert result.processed == 0
    assert archive.last_run()[0] == "complete"
