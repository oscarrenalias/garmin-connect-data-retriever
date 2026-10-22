"""Sequential fetch of pending per-activity resources (SPEC §6.1)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from garmin_archive.api import ApiResponse
from garmin_archive.archive import (
    AVAILABILITY_LISTED,
    AVAILABILITY_NOT_FOUND,
    KIND_ORIGINAL_FILE,
    KIND_SUMMARY,
    KIND_WORKOUTS,
    STATE_FAILED,
    STATE_FORBIDDEN,
    STATE_UNAVAILABLE,
    ArchiveReader,
    ArchiveWriter,
    original_file_key,
    summary_key,
    workouts_key,
)
from garmin_archive.models import (
    ApiTransportError,
    AuthorizationError,
    Clock,
    RateLimitError,
)
from garmin_archive.storage.metadata import bind_account
from garmin_archive.sync.runs import SyncCounters

ERROR_NOT_FOUND = "HTTP_404"
ERROR_FORBIDDEN = "HTTP_403"


class FetchClient(Protocol):
    """The slice of ``GarminApiClient`` that the fetch loop needs."""

    def get_activity(self, activity_id: str) -> ApiResponse: ...

    def get_activity_workouts(self, activity_id: str) -> ApiResponse: ...

    def download_original_file(self, activity_id: str) -> ApiResponse: ...


@dataclass(frozen=True)
class FetchOutcome:
    """Result of a fetch pass; ``interrupted`` and ``rate_limited`` leave work pending."""

    interrupted: bool = False
    rate_limited: RateLimitError | None = None


def _resource(client: FetchClient, kind: str, activity_id: str) -> tuple[str, str, ApiResponse]:
    """Return ``(resource_key, path, response)`` for one resource; may raise API errors."""
    if kind == KIND_SUMMARY:
        return (
            summary_key(activity_id),
            f"/activity-service/activity/{activity_id}",
            (client.get_activity(activity_id)),
        )
    if kind == KIND_WORKOUTS:
        return (
            workouts_key(activity_id),
            f"/activity-service/activity/{activity_id}/workouts",
            client.get_activity_workouts(activity_id),
        )
    if kind == KIND_ORIGINAL_FILE:
        return (
            original_file_key(activity_id),
            f"/download-service/files/activity/{activity_id}",
            client.download_original_file(activity_id),
        )
    raise ValueError(f"unsupported pending kind: {kind!r}")


def _id_text(value: object) -> str | None:
    """Garmin IDs as TEXT; bools and non-integral values are not IDs."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    return None


def parse_summary_identity(body: bytes) -> tuple[str | None, str | None, str | None]:
    """Extract ``(userProfileId, activityTypeDTO.typeKey, parentId)`` from a summary body.

    Each is ``None`` when absent or malformed; an unparseable body yields all ``None``.
    """
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None, None, None
    if not isinstance(parsed, dict):
        return None, None, None
    summary = cast("dict[str, object]", parsed)
    type_dto = summary.get("activityTypeDTO")
    type_key: str | None = None
    if isinstance(type_dto, dict):
        raw_key = cast("dict[str, object]", type_dto).get("typeKey")
        if isinstance(raw_key, str) and raw_key:
            type_key = raw_key
    return _id_text(summary.get("userProfileId")), type_key, _id_text(summary.get("parentId"))


def fetch_pending(
    client: FetchClient,
    conn: sqlite3.Connection,
    writer: ArchiveWriter,
    reader: ArchiveReader,
    clock: Clock,
    run_id: int,
    counters: SyncCounters,
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> FetchOutcome:
    """Fetch every pending resource, one at a time, updating ``counters``.

    No transaction is open during any HTTP call: each writer call commits on its own
    before the next request. 404 and 403 are recorded as ``unavailable``/``forbidden``,
    transport and contract errors as ``failed`` (then the loop continues), and nothing is
    ever deleted. A rate limit stops the pass leaving the rest pending. Auth failures,
    ``AccountMismatchError`` (raised after the offending payload is archived) and
    storage errors propagate.
    """
    for activity_id, kind in reader.pending_resources():
        if should_stop():
            return FetchOutcome(interrupted=True)
        try:
            key, path, response = _resource(client, kind, activity_id)
        except RateLimitError as exc:
            return FetchOutcome(rate_limited=exc)
        except AuthorizationError as exc:
            _record_failure(
                writer, run_id, activity_id, kind, exc.details.get("status"), ERROR_FORBIDDEN
            )
            counters.forbidden += 1
            continue
        except ApiTransportError as exc:  # TransportError and ContractError
            status = exc.details.get("status")
            _record_failure(writer, run_id, activity_id, kind, status, exc.code, STATE_FAILED)
            counters.failed += 1
            continue

        status = response.status
        if status == 404:
            with writer.batch():
                _record_failure(
                    writer, run_id, activity_id, kind, status, ERROR_NOT_FOUND, STATE_UNAVAILABLE
                )
                if kind == KIND_SUMMARY:
                    writer.mark_summary_check(
                        activity_id=activity_id, availability_status=AVAILABILITY_NOT_FOUND
                    )
            counters.unavailable += 1
            continue

        with writer.batch():
            result = writer.record_response(
                resource_key=key,
                kind=kind,
                activity_id=activity_id,
                sync_run_id=run_id,
                method="GET",
                path=path,
                request_params=None,
                http_status=status,
                content_type=response.content_type,
                body=response.body,
            )
            if kind == KIND_SUMMARY:
                writer.mark_summary_check(
                    activity_id=activity_id, availability_status=AVAILABILITY_LISTED
                )
        if result.is_new_payload:
            counters.downloaded += 1
        else:
            counters.unchanged += 1
        if kind == KIND_SUMMARY:
            _apply_summary(writer, conn, clock, activity_id, response.body)
    return FetchOutcome()


def _record_failure(
    writer: ArchiveWriter,
    run_id: int,
    activity_id: str,
    kind: str,
    status: object,
    error_code: str,
    state: str = STATE_FORBIDDEN,
) -> None:
    key = {
        KIND_SUMMARY: summary_key,
        KIND_WORKOUTS: workouts_key,
        KIND_ORIGINAL_FILE: original_file_key,
    }[kind](activity_id)
    paths = {
        KIND_SUMMARY: f"/activity-service/activity/{activity_id}",
        KIND_WORKOUTS: f"/activity-service/activity/{activity_id}/workouts",
        KIND_ORIGINAL_FILE: f"/download-service/files/activity/{activity_id}",
    }
    writer.record_failure(
        resource_key=key,
        kind=kind,
        activity_id=activity_id,
        sync_run_id=run_id,
        method="GET",
        path=paths[kind],
        request_params=None,
        http_status=status if isinstance(status, int) else None,
        error_code=error_code,
        resource_state=state,
    )


def _apply_summary(
    writer: ArchiveWriter,
    conn: sqlite3.Connection,
    clock: Clock,
    activity_id: str,
    body: bytes,
) -> None:
    """Bind/verify the account, then update type and parent from the archived summary."""
    profile_id, type_key, parent_id = parse_summary_identity(body)
    if profile_id is not None:
        bind_account(conn, profile_id, clock)  # raises AccountMismatchError on mismatch
    if type_key is not None:
        writer.update_activity_classification(
            activity_id=activity_id, type_key=type_key, parent_activity_id=parent_id
        )
