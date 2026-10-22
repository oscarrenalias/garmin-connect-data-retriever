"""End-to-end acceptance for spec-8912abbd: FORM import, matching and swim-view precedence."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from garmin_fit_sdk import Encoder

from garmin_archive import ArchiveClient, ArchiveConfig
from garmin_archive.cli import main
from garmin_archive.processing.form_import import FormImportOutcome
from garmin_archive.storage.connection import open_write_connection
from helpers.archive_seed import FixedClock, seed_activity, summary_copy
from helpers.zipbuilder import build_zip

START = datetime(2026, 6, 25, 6, 42, 28, tzinfo=UTC)
SERIAL = 987654321
QUERY_SECRET = "sekrit42"
URL = f"https://formdata-eu-west-1.s3.eu-west-1.amazonaws.com/x.zip?X-Amz-Signature={QUERY_SECRET}"
GARMIN_HR = 141.0
GARMIN_ID = "100"


def _z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def form_fit(
    offset: float = 0,
    *,
    pool_length: float = 25.0,
    sub_sport: str = "lap_swimming",
    manufacturer: str = "form",
) -> bytes:
    """Two 25 m active lengths (one drill), one idle length; serial number in file_id."""
    start = START + timedelta(seconds=offset)
    encoder = Encoder()
    encoder.write_mesg(
        {
            "mesg_num": 0,
            "type": "activity",
            "manufacturer": manufacturer,
            "product": 0,
            "serial_number": SERIAL,
        }
    )
    specs = [("active", "freestyle"), ("active", "drill"), ("idle", None)]
    for i, (kind, stroke) in enumerate(specs):
        message: dict[str, Any] = {
            "mesg_num": 101,
            "start_time": start + timedelta(seconds=30 * i),
            "length_type": kind,
            "total_timer_time": 20.0,
            "total_elapsed_time": 25.0,
        }
        if kind == "active":
            message |= {"swim_stroke": stroke, "total_strokes": 15}
        encoder.write_mesg(message)
    encoder.write_mesg(
        {
            "mesg_num": 18,
            "start_time": start,
            "sport": "swimming",
            "sub_sport": sub_sport,
            "pool_length": pool_length,
            "pool_length_unit": "metric",
            "total_distance": 50.0,
            "total_timer_time": 60.0,
            "total_elapsed_time": 80.0,
            "avg_heart_rate": 120,
            "max_heart_rate": 150,
        }
    )
    encoder.write_mesg(
        {
            "mesg_num": 34,
            "timestamp": start,
            "local_timestamp": datetime(2026, 6, 25, 23, 30, 0, tzinfo=UTC),
        }
    )
    return encoder.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    with ArchiveClient(ArchiveConfig(db_path=path), clock=FixedClock()) as client:
        client.initialize()
    return path


@pytest.fixture
def client(db_path: Path) -> ArchiveClient:
    return ArchiveClient(ArchiveConfig(db_path=db_path), clock=FixedClock())


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_write_connection(db_path, busy_timeout_ms=1000)
    try:
        yield connection
    finally:
        connection.close()


def seed_swim(
    conn: sqlite3.Connection,
    activity_id: str,
    start: datetime,
    *,
    environment: str = "pool",
    pool_length: float = 25.0,
    parent: int = 0,
) -> None:
    """A processed Garmin swim: 1,700 m, 34 lengths, HR 141, plus two Garmin lengths."""
    conn.execute(
        "INSERT INTO activity_archive (activity_id, first_seen_at_utc, last_seen_at_utc)"
        " VALUES (?, 't', 't')",
        (activity_id,),
    )
    run_id = conn.execute(
        "INSERT INTO processing_runs (activity_id, processor_version, input_fingerprint, status,"
        " started_at_utc) VALUES (?, 'v', 'f', 'processed', 't')",
        (activity_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO activities (activity_id, processing_run_id, sport_family, start_at_utc,"
        " local_date, distance_m, timer_duration_s, elapsed_duration_s, avg_hr_bpm,"
        " is_multisport_parent, type_key, processing_status)"
        " VALUES (?, ?, 'swimming', ?, '2026-06-25', 1700, 2400, 2500, ?, ?, ?, 'processed')",
        (
            activity_id,
            run_id,
            _z(start),
            GARMIN_HR,
            parent,
            "lap_swimming" if environment == "pool" else "open_water_swimming",
        ),
    )
    conn.execute(
        "INSERT INTO swimming_metrics (activity_id, processing_run_id, swim_environment,"
        " pool_length_m, active_lengths, total_strokes, active_swim_duration_s,"
        " recorded_rest_duration_s, length_coverage_status)"
        " VALUES (?, ?, ?, ?, 34, 500, 2000, 900, 'complete')",
        (activity_id, run_id, environment, pool_length),
    )
    for index in range(2):
        conn.execute(
            "INSERT INTO swim_lengths (activity_id, length_index, processing_run_id, length_type,"
            " stroke_type, distance_m, timer_duration_s) VALUES (?, ?, ?, 'active', 'freestyle',"
            " 50, 40)",
            (activity_id, index, run_id),
        )
    conn.commit()


def import_fit(client: ArchiveClient, tmp_path: Path, name: str, body: bytes) -> Any:
    path = tmp_path / name
    path.write_bytes(body)
    return client.import_form(paths=[path])


def app_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Log text from this project's loggers (httpx's own INFO line is covered by an xfail below)."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("http"))


def rows(conn: sqlite3.Connection, sql: str, *params: object) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


# --- AC 2: import ----


def test_fit_and_two_file_zip_import_then_duplicates_write_nothing(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    fit = tmp_path / "a.fit"
    fit.write_bytes(form_fit())
    archive = tmp_path / "b.zip"
    archive.write_bytes(build_zip({"one.fit": form_fit(3600), "two.fit": form_fit(7200)}))
    first = client.import_form(paths=[fit, archive])
    assert (first.outcome, first.imported, first.duplicate) == (FormImportOutcome.OK, 3, 0)
    counts = [
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("raw_payloads", "fetch_events")
    ]
    assert counts == [3, 3]
    second = client.import_form(paths=[fit, archive])
    assert (second.imported, second.duplicate) == (0, 3)
    again = [
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("raw_payloads", "fetch_events")
    ]
    assert again == counts


def test_garmin_fit_and_dotdot_zip_member_are_rejected(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    garmin = tmp_path / "g.fit"
    garmin.write_bytes(form_fit(manufacturer="garmin"))
    evil = tmp_path / "evil.zip"
    evil.write_bytes(build_zip({"../escape.fit": form_fit()}))
    result = client.import_form(paths=[garmin, evil])
    assert result.imported == 0
    assert result.outcome is FormImportOutcome.NOTHING_VALID
    assert {r.file.split("/")[0] for r in result.rejected} >= {"g.fit"}
    assert len(result.rejected) >= 2
    assert all(r.reason for r in result.rejected)
    assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 0


# --- AC 3: raw storage and serial privacy ----


def test_stored_bytes_match_input_sha256_and_serial_stays_out_of_derived_tables(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path, db_path: Path
) -> None:
    body = form_fit()
    result = import_fit(client, tmp_path, "a.fit", body)
    digest = hashlib.sha256(body).hexdigest()
    row = conn.execute(
        "SELECT resource_key, sha256, body_blob, kind, activity_id FROM raw_payloads"
    ).fetchone()
    assert bytes(row[2]) == body
    assert row[1] == digest
    assert row[0] == f"imported_file:form:{digest[:16]}"
    assert (row[3], row[4]) == ("imported_file", None)
    # The serial is in the raw FIT bytes only; no derived table holds it.
    needle = str(SERIAL)
    for table in ("form_sessions", "form_lengths", "fetch_events", "activities"):
        dump = json.dumps([list(r) for r in conn.execute(f"SELECT * FROM {table}")], default=str)  # noqa: S608
        assert needle not in dump, table
    for view in ("v_swimming", "v_swim_lengths", "v_activity_summary"):
        dump = json.dumps([list(r) for r in conn.execute(f"SELECT * FROM {view}")], default=str)  # noqa: S608
        assert needle not in dump, view
    assert needle not in json.dumps(result.to_dict())


def test_serial_and_url_absent_from_logs_and_cli_output(
    db_path: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    local = tmp_path / "a.fit"
    local.write_bytes(form_fit())
    bad = tmp_path / "bad.fit"
    bad.write_bytes(form_fit(manufacturer="garmin"))
    zip_body = build_zip({"x.fit": form_fit(3600)})
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=zip_body))
    )
    main(["--db", str(db_path), "import-form", str(local), str(bad)], http_client=http)
    main(["--db", str(db_path), "import-form", "--url", URL], http_client=http)
    out = capsys.readouterr()
    for text in (out.out, out.err, app_log_text(caplog)):
        assert str(SERIAL) not in text
        assert QUERY_SECRET not in text
        assert "X-Amz" not in text


# --- AC 4: --url ----------------------------------------------------------------------------------


def _url_cli(
    db_path: Path,
    url: str,
    handler: Callable[[httpx.Request], httpx.Response],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(recording))
    caplog.set_level(logging.DEBUG)
    code = main(["--db", str(db_path), "import-form", "--url", url], http_client=http)
    out = capsys.readouterr()
    for text in (out.out, out.err, app_log_text(caplog)):
        assert QUERY_SECRET not in text
    return code, seen


def test_url_mock_s3_imports_and_query_string_not_in_db_file(
    db_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    body = build_zip({"x.fit": form_fit()})
    code, seen = _url_cli(db_path, URL, lambda r: httpx.Response(200, content=body), capsys, caplog)
    assert code == 0
    assert len(seen) == 1
    assert "authorization" not in seen[0].headers
    raw = b"".join(p.read_bytes() for p in db_path.parent.glob(f"{db_path.name}*"))
    assert QUERY_SECRET.encode() not in raw
    assert b"X-Amz-Signature" not in raw


@pytest.mark.parametrize(
    "url",
    [
        f"http://formdata-eu-west-1.s3.eu-west-1.amazonaws.com/x.zip?sig={QUERY_SECRET}",
        f"https://evil.example.com/x.zip?sig={QUERY_SECRET}",
        f"https://formdata-eu.s3.eu-west-1.amazonaws.com.evil.test/x.zip?sig={QUERY_SECRET}",
    ],
)
def test_url_refusals_make_no_request(
    db_path: Path,
    url: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    code, seen = _url_cli(db_path, url, lambda r: httpx.Response(200, content=b""), capsys, caplog)
    assert code == 4
    assert seen == []


def test_url_redirect_is_refused_and_not_followed(
    db_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    code, seen = _url_cli(
        db_path,
        URL,
        lambda r: httpx.Response(302, headers={"location": "https://evil.test/?sig=sekrit42"}),
        capsys,
        caplog,
    )
    assert code == 4
    assert len(seen) == 1


# --- AC 5: matching ----


def _match(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT matched_activity_id, match_delta_s, pool_length_mismatch FROM form_sessions"
            " ORDER BY start_at_utc"
        )
    ]


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(8, [(GARMIN_ID, 8.0, 0)]), (660, [(None, None, None)])],
    ids=["8s-matches", "11min-does-not"],
)
def test_match_window(
    client: ArchiveClient,
    conn: sqlite3.Connection,
    tmp_path: Path,
    offset: float,
    expected: list[tuple[Any, ...]],
) -> None:
    seed_swim(conn, GARMIN_ID, START)
    import_fit(client, tmp_path, "a.fit", form_fit(offset))
    assert _match(conn) == expected


def test_two_sessions_near_one_swim_only_closest_matches_and_other_is_in_no_view(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_swim(conn, GARMIN_ID, START)
    import_fit(client, tmp_path, "a.fit", form_fit(8))
    import_fit(client, tmp_path, "b.fit", form_fit(120))
    matched = {
        r[0]: r[1]
        for r in conn.execute("SELECT matched_activity_id, match_delta_s FROM form_sessions")
    }
    assert matched == {GARMIN_ID: 8.0, None: None}
    loser = conn.execute(
        "SELECT form_session_id FROM form_sessions WHERE matched_activity_id IS NULL"
    ).fetchone()[0]
    for view in ("v_swimming", "v_swim_lengths", "v_activity_summary"):
        ids = {r[0] for r in conn.execute(f"SELECT activity_id FROM {view}")}  # noqa: S608
        assert loser not in ids, view
    assert conn.execute("SELECT COUNT(*) FROM v_activity_summary").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_swimming").fetchone()[0] == 1


def test_pool_length_mismatch_is_flagged(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_swim(conn, GARMIN_ID, START, pool_length=50.0)
    import_fit(client, tmp_path, "a.fit", form_fit(8, pool_length=25.0))
    assert _match(conn) == [(GARMIN_ID, 8.0, 1)]


REAL_START = datetime(2026, 3, 29, 7, 51, 47, tzinfo=UTC)  # pool_swim-1 fixture start (GMT)
REAL_OFFSET = (REAL_START - START).total_seconds()


def archive_real_swim(conn: sqlite3.Connection, activity_id: str) -> None:
    seed_activity(conn, FixedClock(), activity_id, summary=summary_copy("pool_swim-1", activity_id))


def test_form_imported_before_garmin_then_reprocess_matches(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    import_fit(client, tmp_path, "a.fit", form_fit(REAL_OFFSET + 8))
    assert _match(conn) == [(None, None, None)]
    archive_real_swim(conn, "300")  # as `update` archives it; update then runs the same pass
    counters = client.reprocess()
    assert (counters.form_matched, counters.form_unmatched) == (1, 0)
    assert _match(conn) == [("300", 8.0, 0)]
    assert (
        rows(conn, "SELECT data_source FROM v_swimming WHERE activity_id = '300'")[0][0] == "form"
    )


def test_reprocess_rebuilds_form_tables_identically(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    archive_real_swim(conn, "300")
    import_fit(client, tmp_path, "a.fit", form_fit(REAL_OFFSET + 8))
    import_fit(client, tmp_path, "b.fit", form_fit(7200))

    def dump() -> list[list[tuple[Any, ...]]]:
        return [
            [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]  # noqa: S608
            for t in ("form_sessions", "form_lengths")
        ]

    client.reprocess()
    before = dump()
    client.reprocess()
    client.reprocess("300")
    assert dump() == before
    assert len(before[0]) == 2
    assert len(before[1]) == 6
    assert sorted(r[0] for r in conn.execute("SELECT form_session_id FROM form_sessions")) == (
        sorted({r[0] for r in conn.execute("SELECT form_session_id FROM form_sessions")})
    )
    assert [r[0] for r in conn.execute("SELECT matched_activity_id FROM form_sessions")].count(
        "300"
    ) == 1


# --- AC 6: precedence ----


def test_matched_pool_swim_precedence_in_all_views(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_swim(conn, GARMIN_ID, START)
    import_fit(client, tmp_path, "a.fit", form_fit(8))
    swim = rows(conn, "SELECT * FROM v_swimming WHERE activity_id = ?", GARMIN_ID)
    assert len(swim) == 1
    s = swim[0]
    assert s["data_source"] == "form"
    assert s["distance_m"] == 50.0
    assert s["active_lengths"] == 2
    assert s["total_strokes"] == 30
    assert s["recorded_rest_duration_s"] == 25.0
    assert s["probable_rest_duration_s"] == 25.0
    assert s["length_coverage_status"] == "complete"
    assert s["likely_unrecorded_swim_duration_s"] is None
    assert s["timer_pace_s_per_100m"] == pytest.approx(60.0 * 100.0 / 50.0)
    assert s["active_pace_s_per_100m"] == pytest.approx(40.0 * 100.0 / 50.0)
    assert (s["garmin_distance_m"], s["garmin_active_lengths"]) == (1700.0, 34)
    assert (s["garmin_active_swim_duration_s"], s["garmin_recorded_rest_duration_s"]) == (
        2000.0,
        900.0,
    )
    assert s["avg_hr_bpm"] == GARMIN_HR

    lengths = rows(conn, "SELECT * FROM v_swim_lengths WHERE activity_id = ?", GARMIN_ID)
    assert {r["data_source"] for r in lengths} == {"form"}
    assert [r["stroke_type"] for r in lengths if r["length_type"] == "active"] == [
        "freestyle",
        "drill",
    ]
    assert len(lengths) == 3
    assert all(r["likely_unrecorded_swim"] is None for r in lengths)

    summary = rows(conn, "SELECT * FROM v_activity_summary WHERE activity_id = ?", GARMIN_ID)
    assert len(summary) == 1
    assert summary[0]["distance_m"] == 1700.0
    assert summary[0]["data_source"] == "garmin"


def test_unmatched_garmin_swim_keeps_garmin_lengths(
    conn: sqlite3.Connection,
) -> None:
    seed_swim(conn, GARMIN_ID, START)
    got = rows(conn, "SELECT data_source, distance_m FROM v_swim_lengths")
    assert [(r["data_source"], r["distance_m"]) for r in got] == [("garmin", 50.0)] * 2
    assert rows(conn, "SELECT data_source FROM v_swimming")[0]["data_source"] == "garmin"


def test_matched_open_water_swim_stays_garmin_everywhere(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_swim(conn, GARMIN_ID, START, environment="open_water", pool_length=0)
    import_fit(client, tmp_path, "a.fit", form_fit(8, sub_sport="open_water"))
    assert _match(conn)[0][0] == GARMIN_ID
    s = rows(conn, "SELECT * FROM v_swimming WHERE activity_id = ?", GARMIN_ID)[0]
    assert (s["data_source"], s["distance_m"], s["active_lengths"]) == ("garmin", 1700.0, 34)
    assert {r["data_source"] for r in rows(conn, "SELECT data_source FROM v_swim_lengths")} == {
        "garmin"
    }
    assert rows(conn, "SELECT data_source FROM v_activity_summary")[0][0] == "garmin"
    assert conn.execute("SELECT COUNT(*) FROM v_swimming").fetchone()[0] == 1


def test_multisport_parent_stays_excluded_from_swim_views(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_swim(conn, "200", START, parent=1)
    assert conn.execute("SELECT COUNT(*) FROM v_swimming").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM v_swim_lengths").fetchone()[0] == 0
    parent = rows(conn, "SELECT include_in_volume_totals FROM v_activity_summary")[0]
    assert parent[0] == 0


# --- AC 7: FORM-only ----


def test_form_only_session_appears_in_all_three_views_with_null_hr(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    import_fit(client, tmp_path, "a.fit", form_fit())
    sid = conn.execute("SELECT form_session_id FROM form_sessions").fetchone()[0]
    assert sid.startswith("form:")
    assert conn.execute("SELECT local_date FROM form_sessions").fetchone()[0] == "2026-06-25"
    swim = rows(conn, "SELECT * FROM v_swimming")
    assert len(swim) == 1
    assert (swim[0]["activity_id"], swim[0]["data_source"]) == (sid, "form")
    assert swim[0]["avg_hr_bpm"] is None
    assert swim[0]["type_key"] == "lap_swimming"
    assert swim[0]["sport_family"] == "swimming"
    assert swim[0]["distance_m"] == 50.0
    lengths = rows(conn, "SELECT * FROM v_swim_lengths")
    assert {(r["activity_id"], r["data_source"]) for r in lengths} == {(sid, "form")}
    assert len(lengths) == 3
    summary = rows(conn, "SELECT * FROM v_activity_summary")
    assert len(summary) == 1
    assert (summary[0]["data_source"], summary[0]["include_in_volume_totals"]) == ("form", 1)
    assert summary[0]["avg_hr_bpm"] is None


def test_unmatched_session_overlapping_garmin_swim_in_other_environment_is_absent(
    client: ArchiveClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_swim(conn, GARMIN_ID, START, environment="open_water", pool_length=0)
    import_fit(client, tmp_path, "a.fit", form_fit(8))  # pool session; environments differ
    assert _match(conn) == [(None, None, None)]
    for view in ("v_swimming", "v_swim_lengths", "v_activity_summary"):
        sources = {r[0] for r in conn.execute(f"SELECT data_source FROM {view}")}  # noqa: S608
        assert sources == {"garmin"}, view


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: httpx logs 'HTTP Request: GET <full URL>' at INFO, so with INFO logging "
    "enabled the signed query string reaches the logs (spec AC 4)",
)
def test_url_query_string_absent_from_httpx_info_logs(
    db_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    body = build_zip({"x.fit": form_fit()})
    _url_cli(db_path, URL, lambda r: httpx.Response(200, content=body), capsys, caplog)
    assert QUERY_SECRET not in caplog.text
