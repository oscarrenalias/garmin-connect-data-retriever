"""Per-activity processing with atomic replacement of derived rows (SPEC §7).

``process_activity`` records a ``processing_runs`` row, builds every derived row in
memory and then replaces all of the activity's derived rows in one transaction. A
failure while replacing rolls back, so the previous rows stay intact, and the run is
recorded as ``failed`` with a stable error code. Raw payloads are only read.

Each activity is processed from its own inputs: a multisport parent's FIT sessions are
stored on the parent only and are never copied onto its children.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from garmin_archive.archive import RawPayload
from garmin_archive.models import Clock, GarminArchiveError, StorageError, utc_rfc3339
from garmin_archive.processing.activity_row import build_activity_row
from garmin_archive.processing.fit_rows import lap_rows, sample_rows, session_rows, set_rows
from garmin_archive.processing.fitfile import (
    FitMessages,
    FitUnavailableError,
    decode_fit,
    extract_fit,
)
from garmin_archive.processing.inputs import ProcessingInputs, load_inputs, processor_version
from garmin_archive.processing.metrics import (
    cardio_metrics_row,
    cycling_metrics_row,
    running_metrics_row,
    swim_pool_length_m,
    swimming_metrics_row,
)
from garmin_archive.processing.swim import swim_length_rows
from garmin_archive.storage.connection import transaction

STATUS_PROCESSED = "processed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

ERROR_PROCESSING = "PROCESSING_ERROR"

# Child tables first so foreign keys hold while deleting.
_DERIVED_TABLES = (
    "swim_lengths",
    "running_metrics",
    "swimming_metrics",
    "cycling_metrics",
    "cardio_metrics",
    "activity_sets",
    "activity_samples",
    "activity_laps",
    "activity_sessions",
    "activities",
)


@dataclass(frozen=True)
class ProcessingResult:
    """Outcome of one ``process_activity`` call."""

    activity_id: str
    run_id: int
    status: str
    warnings: tuple[str, ...]
    error_code: str | None = None


def _json_object(payload: RawPayload | None, label: str, warnings: list[str]) -> dict[str, Any]:
    if payload is None:
        warnings.append(f"{label}_missing")
        return {}
    try:
        parsed = cast("object", json.loads(payload.body_blob))
    except ValueError:
        warnings.append(f"{label}_not_json")
        return {}
    if not isinstance(parsed, dict):
        warnings.append(f"{label}_not_object")
        return {}
    return cast("dict[str, Any]", parsed)


def _activity_row(
    inputs: ProcessingInputs, warnings: list[str]
) -> tuple[dict[str, Any], int | None, dict[str, Any]]:
    summary = _json_object(inputs.summary, "summary", warnings)
    item = inputs.search_item
    search_item = cast("Mapping[str, Any]", item) if isinstance(item, dict) else None
    if "activityId" not in summary and search_item is None:
        summary = {**summary, "activityId": inputs.activity_id}
    row = build_activity_row(summary, search_item)
    row["activity_id"] = inputs.activity_id
    source = inputs.summary_payload_id
    if source is None:
        source = inputs.search_item_payload_id
    return row, source, summary


def _fit_messages(file: RawPayload | None, warnings: list[str]) -> FitMessages | None:
    if file is None:
        warnings.append("original_file_missing")
        return None
    try:
        return decode_fit(extract_fit(file.body_blob))
    except FitUnavailableError as exc:
        warnings.append(f"fit_unavailable: {exc}")
        return None


def _insert(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    common: Mapping[str, Any],
) -> None:
    if not rows:
        return
    columns = [*common, *rows[0]]
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})"
    )
    conn.executemany(sql, [{**common, **row} for row in rows])


@dataclass(frozen=True)
class _SportRows:
    """Sport-specific derived rows: at most one metrics row plus swim lengths."""

    metrics_table: str | None
    metrics_row: dict[str, Any] | None
    lengths: list[dict[str, Any]]


_NO_SPORT_ROWS = _SportRows(None, None, [])


def _sport_rows(
    activity: Mapping[str, Any],
    summary: Mapping[str, Any],
    fit: FitMessages | None,
    warnings: list[str],
) -> _SportRows:
    family = activity.get("sport_family")
    if family == "running":
        return _SportRows("running_metrics", running_metrics_row(summary), [])
    if family == "cycling":
        return _SportRows("cycling_metrics", cycling_metrics_row(summary), [])
    if family == "cardio":
        sets = set_rows(fit.sets) if fit is not None else []
        return _SportRows("cardio_metrics", cardio_metrics_row(summary, sets), [])
    if family == "swimming":
        type_key = activity.get("type_key")
        pool = type_key == "lap_swimming"
        pool_length = swim_pool_length_m(summary, warnings) if pool else None
        lengths = (
            swim_length_rows(fit.lengths, fit.laps, pool_length) if pool and fit is not None else []
        )
        row = swimming_metrics_row(str(type_key), summary, lengths, warnings)
        return _SportRows("swimming_metrics", row, lengths)
    return _NO_SPORT_ROWS


def _replace_rows(
    conn: sqlite3.Connection,
    activity_id: str,
    run_id: int,
    activity: dict[str, Any],
    activity_source: int | None,
    summary_source: int | None,
    fit: FitMessages | None,
    file_source: int | None,
    sport: _SportRows,
    status: str,
    now: str,
) -> None:
    for table in _DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE activity_id = ?", (activity_id,))
    row = {
        **activity,
        "processing_run_id": run_id,
        "source_payload_id": activity_source,
        "processing_status": status,
        "processed_at_utc": now,
    }
    _insert(conn, "activities", [row], {})
    if sport.metrics_table is not None and sport.metrics_row is not None:
        metrics_common = {
            "activity_id": activity_id,
            "processing_run_id": run_id,
            "source_payload_id": summary_source,
        }
        _insert(conn, sport.metrics_table, [sport.metrics_row], metrics_common)
    if fit is None:
        return
    common = {
        "activity_id": activity_id,
        "processing_run_id": run_id,
        "source_payload_id": file_source,
    }
    sessions = session_rows(fit.sessions)
    _insert(conn, "activity_sessions", sessions, common)
    _insert(conn, "activity_laps", lap_rows(fit.laps, fit.sessions) if sessions else [], common)
    _insert(
        conn,
        "activity_samples",
        sample_rows(fit.records, running=activity.get("sport_family") == "running"),
        common,
    )
    _insert(conn, "activity_sets", set_rows(fit.sets), common)
    _insert(conn, "swim_lengths", sport.lengths, common)


def _warnings_json(warnings: Sequence[str]) -> str | None:
    return json.dumps(list(warnings)) if warnings else None


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, GarminArchiveError):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return StorageError.code
    return ERROR_PROCESSING


def process_activity(conn: sqlite3.Connection, activity_id: str, clock: Clock) -> ProcessingResult:
    """Rebuild every derived row of ``activity_id`` from its current raw payloads.

    Returns the recorded run outcome. Unexpected errors are recorded on the run as
    ``failed`` (error code only, no message) and not raised; the previous derived rows
    are left untouched.
    """
    started = utc_rfc3339(clock.now())
    version = processor_version()
    try:
        inputs = load_inputs(conn, activity_id)
        with transaction(conn):
            cursor = conn.execute(
                "INSERT INTO processing_runs"
                " (activity_id, processor_version, input_fingerprint, status, started_at_utc)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (activity_id, version, inputs.fingerprint, started),
            )
            run_id = int(cursor.lastrowid or 0)
    except sqlite3.Error as exc:
        raise StorageError(f"Could not record processing run: {exc}") from exc

    warnings: list[str] = []
    try:
        activity, activity_source, summary = _activity_row(inputs, warnings)
        fit = _fit_messages(inputs.original_file, warnings)
        if fit is not None and fit.laps and not fit.sessions:
            warnings.append("laps_without_session")
        sport = _sport_rows(activity, summary, fit, warnings)
        status = STATUS_PARTIAL if warnings else STATUS_PROCESSED
        finished = utc_rfc3339(clock.now())
        with transaction(conn):
            _replace_rows(
                conn,
                activity_id,
                run_id,
                activity,
                activity_source,
                inputs.summary_payload_id,
                fit,
                inputs.file_payload_id,
                sport,
                status,
                finished,
            )
            conn.execute(
                "UPDATE processing_runs SET status = ?, finished_at_utc = ?, warnings_json = ?"
                " WHERE id = ?",
                (status, finished, _warnings_json(warnings), run_id),
            )
    except Exception as exc:
        code = _error_code(exc)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE processing_runs SET status = ?, finished_at_utc = ?,"
                    " warnings_json = ?, error_code = ? WHERE id = ?",
                    (
                        STATUS_FAILED,
                        utc_rfc3339(clock.now()),
                        _warnings_json(warnings),
                        code,
                        run_id,
                    ),
                )
        except sqlite3.Error as inner:
            raise StorageError(f"Could not record failed processing run: {inner}") from inner
        return ProcessingResult(activity_id, run_id, STATUS_FAILED, tuple(warnings), code)
    return ProcessingResult(activity_id, run_id, status, tuple(warnings))
