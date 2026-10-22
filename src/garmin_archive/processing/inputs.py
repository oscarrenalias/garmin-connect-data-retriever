"""Processing inputs, processor version and input fingerprint (SPEC §7).

Read-only: nothing here writes to the archive or modifies raw rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from garmin_archive._version import __version__
from garmin_archive.archive import (
    ACTIVITY_RESOURCE_KIND_SEARCH_ITEM,
    KIND_ORIGINAL_FILE,
    KIND_SUMMARY,
    ArchiveReader,
    RawPayload,
    canonical_json_hash,
)
from garmin_archive.models import StorageError
from garmin_archive.processing.sports import SPORTS_VERSION

PROCESSED_STATUSES = ("processed", "partial")


def processor_version() -> str:
    """Return the package version combined with ``SPORTS_VERSION``."""
    return f"{__version__}+sports{SPORTS_VERSION}"


def input_fingerprint(
    summary_payload_id: int | None,
    file_payload_id: int | None,
    version: str | None = None,
) -> str:
    """Hash the summary payload ID, file payload ID and processor version.

    The search item is deliberately excluded: it is only a fallback source.
    """
    return canonical_json_hash(
        [
            summary_payload_id,
            file_payload_id,
            version if version is not None else processor_version(),
        ]
    )


@dataclass(frozen=True)
class ProcessingInputs:
    """Current archived inputs for one activity; any part may be absent."""

    activity_id: str
    summary: RawPayload | None
    original_file: RawPayload | None
    search_item: object | None
    search_item_payload_id: int | None

    @property
    def summary_payload_id(self) -> int | None:
        return None if self.summary is None else self.summary.id

    @property
    def file_payload_id(self) -> int | None:
        return None if self.original_file is None else self.original_file.id

    @property
    def fingerprint(self) -> str:
        return input_fingerprint(self.summary_payload_id, self.file_payload_id)


def load_inputs(conn: sqlite3.Connection, activity_id: str) -> ProcessingInputs:
    """Load the current summary, latest search item and current original file."""
    reader = ArchiveReader(conn)
    try:
        rows = conn.execute(
            "SELECT current_payload_id FROM activity_resources WHERE activity_id = ? AND kind = ?",
            (activity_id, ACTIVITY_RESOURCE_KIND_SEARCH_ITEM),
        ).fetchall()
    except sqlite3.Error as exc:
        raise StorageError(f"Archive read failed: {exc}") from exc
    search_payload_id = int(rows[0][0]) if rows and rows[0][0] is not None else None
    return ProcessingInputs(
        activity_id=activity_id,
        summary=reader.current_payload(activity_id, KIND_SUMMARY),
        original_file=reader.current_payload(activity_id, KIND_ORIGINAL_FILE),
        search_item=reader.prior_search_item(activity_id),
        search_item_payload_id=search_payload_id,
    )


def pending_activity_ids(conn: sqlite3.Connection) -> list[str]:
    """Return activity IDs whose fingerprint differs from their latest processed/partial run.

    Activities with no processed or partial run are included; a partial run with unchanged
    inputs is not.
    """
    version = processor_version()
    placeholders = ",".join("?" for _ in PROCESSED_STATUSES)
    try:
        activities = conn.execute(
            "SELECT a.activity_id,"
            " (SELECT current_payload_id FROM activity_resources r"
            "   WHERE r.activity_id = a.activity_id AND r.kind = ?),"
            " (SELECT current_payload_id FROM activity_resources r"
            "   WHERE r.activity_id = a.activity_id AND r.kind = ?)"
            " FROM activity_archive a ORDER BY a.activity_id",
            (KIND_SUMMARY, KIND_ORIGINAL_FILE),
        ).fetchall()
        latest = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT p.activity_id, p.input_fingerprint FROM processing_runs p"
                f" WHERE p.status IN ({placeholders}) AND p.id = ("
                "   SELECT q.id FROM processing_runs q"
                f"   WHERE q.activity_id = p.activity_id AND q.status IN ({placeholders})"
                "   ORDER BY q.started_at_utc DESC, q.id DESC LIMIT 1)",
                PROCESSED_STATUSES * 2,
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise StorageError(f"Archive read failed: {exc}") from exc
    return [
        str(activity_id)
        for activity_id, summary_id, file_id in activities
        if latest.get(str(activity_id)) != input_fingerprint(summary_id, file_id, version)
    ]
