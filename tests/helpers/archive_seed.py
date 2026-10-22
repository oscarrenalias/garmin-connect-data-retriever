"""Seed an archive database with raw payloads for processing tests. No network, no files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from garmin_archive.archive import (
    KIND_ORIGINAL_FILE,
    KIND_SEARCH_PAGE,
    KIND_SUMMARY,
    ArchiveWriter,
    original_file_key,
    search_page_key,
    summary_key,
)

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "contract"
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


class FixedClock:
    """Injectable clock whose time is set by the test."""

    def __init__(self, now: datetime = T0) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def load_fixture(kind: str, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (CONTRACT_DIR / kind / f"{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def summary_copy(name: str, activity_id: str, **overrides: Any) -> bytes:
    """A copy of a committed sanitized summary fixture with a synthetic activity ID."""
    summary = load_fixture("summaries", name)
    summary["activityId"] = int(activity_id)
    summary.update(overrides)
    return json.dumps(summary, indent=2).encode("utf-8")


def search_item_copy(name: str, activity_id: str) -> dict[str, Any]:
    item = load_fixture("search-items", name)
    item["activityId"] = int(activity_id)
    return item


def seed_activity(
    conn: sqlite3.Connection,
    clock: FixedClock,
    activity_id: str,
    *,
    summary: bytes | None = None,
    original_file: bytes | None = None,
    search_item: Mapping[str, Any] | None = None,
    type_key: str | None = None,
    parent_activity_id: str | None = None,
) -> None:
    """Archive an activity row plus whichever raw payloads are given."""
    writer = ArchiveWriter(conn, clock)
    writer.upsert_activity(
        activity_id=activity_id,
        owner_profile_id=None,
        parent_activity_id=parent_activity_id,
        type_key=type_key,
    )
    if summary is not None:
        record_summary(conn, clock, activity_id, summary)
    if original_file is not None:
        record_file(conn, clock, activity_id, original_file)
    if search_item is not None:
        params: dict[str, str | int | bool] = {"start": int(activity_id) % 100000, "limit": 1}
        body = json.dumps([search_item]).encode("utf-8")
        result = writer.record_response(
            resource_key=search_page_key(params),
            kind=KIND_SEARCH_PAGE,
            activity_id=None,
            sync_run_id=None,
            method="GET",
            path="/search",
            request_params=params,
            http_status=200,
            content_type="application/json",
            body=body,
        )
        writer.record_search_item(
            activity_id=activity_id, search_page_payload_id=result.payload_id, json_pointer="/0"
        )


def record_summary(
    conn: sqlite3.Connection, clock: FixedClock, activity_id: str, body: bytes
) -> int:
    return (
        ArchiveWriter(conn, clock)
        .record_response(
            resource_key=summary_key(activity_id),
            kind=KIND_SUMMARY,
            activity_id=activity_id,
            sync_run_id=None,
            method="GET",
            path=f"/activity-service/activity/{activity_id}",
            request_params=None,
            http_status=200,
            content_type="application/json",
            body=body,
        )
        .payload_id
    )


def record_file(conn: sqlite3.Connection, clock: FixedClock, activity_id: str, body: bytes) -> int:
    return (
        ArchiveWriter(conn, clock)
        .record_response(
            resource_key=original_file_key(activity_id),
            kind=KIND_ORIGINAL_FILE,
            activity_id=activity_id,
            sync_run_id=None,
            method="GET",
            path=f"/download-service/files/activity/{activity_id}",
            request_params=None,
            http_status=200,
            content_type="application/zip",
            body=body,
        )
        .payload_id
    )


def raw_payload_digest(conn: sqlite3.Connection) -> str:
    """SHA-256 over every raw payload row's id, key and body bytes, in id order."""
    digest = hashlib.sha256()
    for payload_id, key, sha, blob in conn.execute(
        "SELECT id, resource_key, sha256, body_blob FROM raw_payloads ORDER BY id"
    ):
        digest.update(f"{payload_id}|{key}|{sha}|".encode())
        digest.update(bytes(blob))
    return digest.hexdigest()
