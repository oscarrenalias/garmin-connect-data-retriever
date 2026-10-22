"""Acceptance criterion 8 of spec-2699f0ff: stable views over the sport tables."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.storage.connection import open_write_connection

VIEWS = (
    "v_activity_summary",
    "v_running",
    "v_swimming",
    "v_swim_lengths",
    "v_cycling",
    "v_cardio",
)


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "a.db"
    ArchiveClient(ArchiveConfig(db_path=path), clock=FixedClock(), environ={}).initialize()
    c = open_write_connection(path, busy_timeout_ms=1000)
    c.execute(
        "INSERT INTO activity_archive (activity_id, first_seen_at_utc, last_seen_at_utc)"
        " VALUES ('run0', 't', 't')"
    )
    c.execute(
        "INSERT INTO processing_runs (id, activity_id, processor_version, input_fingerprint,"
        " status, started_at_utc) VALUES (1, 'run0', 'v', 'f', 'processed', 't')"
    )
    yield c
    c.close()


def _activity(
    conn: sqlite3.Connection,
    activity_id: str,
    family: str,
    *,
    distance: float | None = None,
    timer: float | None = None,
    parent: int | None = 0,
) -> None:
    conn.execute(
        "INSERT INTO activity_archive (activity_id, first_seen_at_utc, last_seen_at_utc)"
        " VALUES (?, 't', 't')",
        (activity_id,),
    )
    conn.execute(
        "INSERT INTO activities (activity_id, processing_run_id, is_multisport_parent,"
        " sport_family, local_date, distance_m, timer_duration_s, processing_status)"
        " VALUES (?, 1, ?, ?, '2026-06-25', ?, ?, 'processed')",
        (activity_id, parent, family, distance, timer),
    )


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> object:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_is_queryable_on_an_empty_database(conn: sqlite3.Connection, view: str) -> None:
    assert conn.execute(f"SELECT * FROM {view}").fetchall() == []


def test_views_exist_in_schema(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert names == set(VIEWS)


def test_running_pace(conn: sqlite3.Connection) -> None:
    _activity(conn, "r1", "running", distance=5000.0, timer=1500.0)
    conn.execute("INSERT INTO running_metrics (activity_id, processing_run_id) VALUES ('r1', 1)")
    assert _scalar(conn, "SELECT timer_pace_s_per_km FROM v_running") == pytest.approx(300.0)


@pytest.mark.parametrize("distance", [0.0, None])
def test_running_pace_null_without_distance(
    conn: sqlite3.Connection, distance: float | None
) -> None:
    _activity(conn, "r1", "running", distance=distance, timer=1500.0)
    conn.execute("INSERT INTO running_metrics (activity_id, processing_run_id) VALUES ('r1', 1)")
    assert _scalar(conn, "SELECT timer_pace_s_per_km FROM v_running") is None


def test_swimming_paces(conn: sqlite3.Connection) -> None:
    _activity(conn, "s1", "swimming", distance=1000.0, timer=2000.0)
    conn.execute(
        "INSERT INTO swimming_metrics (activity_id, processing_run_id, active_swim_duration_s)"
        " VALUES ('s1', 1, 1500.0)"
    )
    row = conn.execute(
        "SELECT timer_pace_s_per_100m, active_pace_s_per_100m FROM v_swimming"
    ).fetchone()
    assert row == (pytest.approx(200.0), pytest.approx(150.0))


def test_swimming_active_pace_null_when_active_duration_unknown(conn: sqlite3.Connection) -> None:
    _activity(conn, "s1", "swimming", distance=1000.0, timer=2000.0)
    conn.execute("INSERT INTO swimming_metrics (activity_id, processing_run_id) VALUES ('s1', 1)")
    row = conn.execute(
        "SELECT timer_pace_s_per_100m, active_pace_s_per_100m FROM v_swimming"
    ).fetchone()
    assert row == (pytest.approx(200.0), None)


@pytest.mark.parametrize("distance", [0.0, None])
def test_swimming_paces_null_without_distance(
    conn: sqlite3.Connection, distance: float | None
) -> None:
    _activity(conn, "s1", "swimming", distance=distance, timer=2000.0)
    conn.execute(
        "INSERT INTO swimming_metrics (activity_id, processing_run_id, active_swim_duration_s)"
        " VALUES ('s1', 1, 1500.0)"
    )
    row = conn.execute(
        "SELECT timer_pace_s_per_100m, active_pace_s_per_100m FROM v_swimming"
    ).fetchone()
    assert row == (None, None)


def test_swim_length_paces(conn: sqlite3.Connection) -> None:
    _activity(conn, "s1", "swimming")
    rows = [
        (0, "active", 25.0, 20.0),
        (1, "idle", None, 15.0),
        (2, "active", 25.0, 31.25),
        (3, "active", 0.0, 10.0),
        (4, "active", None, 10.0),
    ]
    for index, kind, distance, timer in rows:
        conn.execute(
            "INSERT INTO swim_lengths (activity_id, length_index, processing_run_id, length_type,"
            " distance_m, timer_duration_s) VALUES ('s1', ?, 1, ?, ?, ?)",
            (index, kind, distance, timer),
        )
    paces = conn.execute(
        "SELECT length_index, pace_s_per_100m, local_date FROM v_swim_lengths ORDER BY length_index"
    ).fetchall()
    assert paces == [
        (0, pytest.approx(80.0), "2026-06-25"),
        (1, None, "2026-06-25"),
        (2, pytest.approx(125.0), "2026-06-25"),
        (3, None, "2026-06-25"),
        (4, None, "2026-06-25"),
    ]


def test_cycling_and_cardio_views_join_metrics(conn: sqlite3.Connection) -> None:
    _activity(conn, "c1", "cycling")
    _activity(conn, "k1", "cardio")
    conn.execute(
        "INSERT INTO cycling_metrics (activity_id, processing_run_id, avg_power_w)"
        " VALUES ('c1', 1, 200.0)"
    )
    conn.execute(
        "INSERT INTO cardio_metrics (activity_id, processing_run_id, steps) VALUES ('k1', 1, 99)"
    )
    assert conn.execute("SELECT activity_id, avg_power_w FROM v_cycling").fetchall() == [
        ("c1", 200.0)
    ]
    assert conn.execute("SELECT activity_id, steps FROM v_cardio").fetchall() == [("k1", 99)]


def test_multisport_parent_flagged_and_excluded_from_sport_views(
    conn: sqlite3.Connection,
) -> None:
    _activity(conn, "p1", "swimming", distance=1000.0, timer=100.0, parent=1)
    _activity(conn, "s1", "swimming", distance=1000.0, timer=100.0, parent=0)
    _activity(conn, "n1", "swimming", distance=1000.0, timer=100.0, parent=None)
    for aid in ("p1", "s1", "n1"):
        conn.execute(
            "INSERT INTO swimming_metrics (activity_id, processing_run_id) VALUES (?, 1)", (aid,)
        )
        conn.execute(
            "INSERT INTO swim_lengths (activity_id, length_index, processing_run_id)"
            " VALUES (?, 0, 1)",
            (aid,),
        )
    flags = dict(
        conn.execute("SELECT activity_id, include_in_volume_totals FROM v_activity_summary")
    )
    assert flags == {"p1": 0, "s1": 1, "n1": 1}
    assert {r[0] for r in conn.execute("SELECT activity_id FROM v_swimming")} == {"s1", "n1"}
    assert {r[0] for r in conn.execute("SELECT activity_id FROM v_swim_lengths")} == {"s1", "n1"}


def test_multisport_parent_excluded_from_every_sport_view(conn: sqlite3.Connection) -> None:
    for aid, family, table in (
        ("a", "running", "running_metrics"),
        ("b", "cycling", "cycling_metrics"),
        ("c", "cardio", "cardio_metrics"),
    ):
        _activity(conn, aid, family, parent=1)
        conn.execute(f"INSERT INTO {table} (activity_id, processing_run_id) VALUES (?, 1)", (aid,))
    for view in ("v_running", "v_cycling", "v_cardio"):
        assert conn.execute(f"SELECT * FROM {view}").fetchall() == [], view
    assert _scalar(conn, "SELECT COUNT(*) FROM v_activity_summary") == 3


def test_sport_views_exclude_other_families_and_metricless_activities(
    conn: sqlite3.Connection,
) -> None:
    _activity(conn, "h1", "hiit")
    _activity(conn, "w1", "walking")
    _activity(conn, "r1", "running")  # no metrics row
    assert _scalar(conn, "SELECT COUNT(*) FROM v_activity_summary") == 3
    assert _scalar(conn, "SELECT COUNT(*) FROM v_running") == 0
