"""Build ``form_sessions`` and ``form_lengths`` rows from one FORM goggles FIT payload.

Pure functions: no I/O, no clock, no host timezone. Absent values stay ``None``. The device
serial number is never read. Matching columns (``matched_activity_id``, ``match_delta_s``,
``pool_length_mismatch``) are left to the matching pass.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from garmin_fit_sdk import Decoder, Stream

from garmin_archive.processing.fit_rows import number, session_index, text, to_utc_z, window
from garmin_archive.processing.fitfile import FitUnavailableError
from garmin_archive.processing.form_match import FormSwim, GarminSwim, match_form_sessions
from garmin_archive.processing.swim import swim_length_rows
from garmin_archive.storage.connection import transaction

_YARD_M = 0.9144
_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
_ENVIRONMENTS = {"lap_swimming": "pool", "open_water": "open_water"}


def _pool_length_m(session: Mapping[str, Any]) -> float | None:
    """``pool_length`` in metres: ``statute`` means yards, ``metric`` (or no unit) metres."""
    length = number(session.get("pool_length"))
    if length is None:
        return None
    unit = text(session.get("pool_length_unit"))
    return float(length) * _YARD_M if unit == "statute" else float(length)


def _local_date(activity: Mapping[str, Any] | None) -> str | None:
    """Calendar date of ``local_timestamp``; the SDK value already reads as local wall time."""
    value = None if activity is None else activity.get("local_timestamp")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, int) and not isinstance(value, bool):
        return (_FIT_EPOCH + timedelta(seconds=value)).date().isoformat()
    return None


def _sum(rows: Sequence[Mapping[str, Any]], length_type: str, key: str) -> float | None:
    values = [
        row[key]
        for row in rows
        if row["length_type"] == length_type and isinstance(row[key], int | float)
    ]
    return float(sum(values)) if values else None


def parse_form_fit(
    data: bytes, payload_id: int, sha256: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(form_sessions rows, form_lengths rows)`` for one FORM FIT file.

    ``form_session_id`` is ``form:<sha256[:16]>``, with ``:<session index>`` appended when
    the file holds more than one session. Raises ``FitUnavailableError`` if undecodable.
    """
    try:
        decoder = Decoder(Stream.from_byte_array(bytearray(data)))
        raw, errors = decoder.read()
    except Exception as exc:
        raise FitUnavailableError("FIT file could not be decoded") from exc
    if errors:
        raise FitUnavailableError("FIT file could not be decoded")
    messages = cast("dict[str, list[dict[str, Any]]]", raw)
    sessions = messages.get("session_mesgs", [])
    laps = messages.get("lap_mesgs", [])
    lengths = messages.get("length_mesgs", [])
    activities = messages.get("activity_mesgs", [])
    local_date = _local_date(activities[0] if activities else None)

    windows = [window(message) for message in sessions]
    grouped: list[list[Mapping[str, Any]]] = [[] for _ in sessions]
    for message in lengths:
        index = session_index(message.get("start_time"), windows)
        grouped[index].append(message)

    base = f"form:{sha256[:16]}"
    session_rows: list[dict[str, Any]] = []
    length_rows: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        session_id = base if len(sessions) == 1 else f"{base}:{index}"
        pool = _pool_length_m(session)
        rows = swim_length_rows(grouped[index], laps, pool)
        active = [row for row in rows if row["length_type"] == "active"]
        strokes = [row["stroke_count"] for row in active if row["stroke_count"] is not None]
        active_lengths = number(session.get("num_active_lengths"))
        session_rows.append(
            {
                "form_session_id": session_id,
                "source_payload_id": payload_id,
                "session_index": index,
                "start_at_utc": to_utc_z(session.get("start_time")),
                "local_date": local_date,
                "environment": _ENVIRONMENTS.get(text(session.get("sub_sport")) or ""),
                "pool_length_m": pool,
                "distance_m": number(session.get("total_distance")),
                "active_lengths": len(active) if rows else _int(active_lengths),
                "active_swim_duration_s": _sum(rows, "active", "timer_duration_s"),
                "rest_duration_s": _sum(rows, "idle", "elapsed_duration_s"),
                "timer_duration_s": number(session.get("total_timer_time")),
                "elapsed_duration_s": number(session.get("total_elapsed_time")),
                "total_strokes": sum(strokes) if strokes else None,
                "avg_hr_bpm": number(session.get("avg_heart_rate")),
                "max_hr_bpm": number(session.get("max_heart_rate")),
            }
        )
        for row in rows:
            length_rows.append(
                {
                    "form_session_id": session_id,
                    "source_payload_id": payload_id,
                    **row,
                    "likely_unrecorded_swim": None,
                }
            )
    return session_rows, length_rows


def _int(value: float | int | None) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class FormRebuildResult:
    """Counts for one ``rebuild_form`` run; ``skipped`` counts undecodable payloads."""

    matched: int
    unmatched: int
    skipped: int = 0


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _garmin_swims(conn: sqlite3.Connection) -> list[GarminSwim]:
    rows = conn.execute(
        "SELECT a.activity_id, a.start_at_utc, a.elapsed_duration_s, a.timer_duration_s,"
        " s.swim_environment, s.pool_length_m FROM activities a"
        " JOIN swimming_metrics s ON s.activity_id = a.activity_id"
        " WHERE s.swim_environment IS NOT NULL ORDER BY a.activity_id"
    ).fetchall()
    swims: list[GarminSwim] = []
    for activity_id, start_text, elapsed, timer, environment, pool in rows:
        start = _parse_utc(start_text)
        duration = elapsed if elapsed is not None else timer
        if start is None or duration is None:
            continue
        swims.append(
            GarminSwim(
                str(activity_id), start, start + timedelta(seconds=duration), environment, pool
            )
        )
    return swims


def rebuild_form(conn: sqlite3.Connection) -> FormRebuildResult:
    """Rebuild ``form_sessions`` and ``form_lengths`` wholesale from imported FORM payloads.

    One transaction: delete every row, re-parse each ``imported_file:form:%`` payload, match
    the sessions to the current Garmin swims and insert. Writes no ``processing_runs`` rows;
    local only (no network, no credentials). Undecodable payloads are skipped and counted.
    """
    payloads = conn.execute(
        "SELECT id, sha256, body_blob FROM raw_payloads"
        " WHERE resource_key LIKE 'imported_file:form:%' ORDER BY id"
    ).fetchall()
    with transaction(conn):
        conn.execute("DELETE FROM form_lengths")
        conn.execute("DELETE FROM form_sessions")
        garmin = _garmin_swims(conn)
        sessions: dict[str, dict[str, Any]] = {}
        lengths: list[dict[str, Any]] = []
        skipped = 0
        for payload_id, sha256, blob in payloads:
            try:
                session_rows, length_rows = parse_form_fit(bytes(blob), int(payload_id), sha256)
            except FitUnavailableError:
                skipped += 1
                continue
            for row in session_rows:
                if row["form_session_id"] in sessions:
                    continue
                sessions[row["form_session_id"]] = row
            lengths.extend(row for row in length_rows if row["form_session_id"] in sessions)
        candidates: list[FormSwim] = []
        for row in sessions.values():
            start = _parse_utc(row["start_at_utc"])
            if start is None or row["environment"] is None:
                continue
            seconds = row["elapsed_duration_s"]
            if seconds is None:
                seconds = row["timer_duration_s"]
            end = start + timedelta(seconds=seconds or 0)
            candidates.append(
                FormSwim(
                    row["form_session_id"], start, end, row["environment"], row["pool_length_m"]
                )
            )
        matches = {m.form_session_id: m for m in match_form_sessions(candidates, garmin)}
        matched = 0
        for session_id, row in sessions.items():
            match = matches.get(session_id)
            row["matched_activity_id"] = match.matched_activity_id if match else None
            row["match_delta_s"] = match.match_delta_s if match else None
            row["pool_length_mismatch"] = (
                match.pool_length_mismatch if match and match.matched_activity_id else None
            )
            matched += row["matched_activity_id"] is not None
            _insert(conn, "form_sessions", row)
        for row in lengths:
            _insert(conn, "form_lengths", row)
    return FormRebuildResult(matched=matched, unmatched=len(sessions) - matched, skipped=skipped)


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))  # noqa: S608
