"""Match FORM goggle sessions to Garmin swims.

Pure functions: no I/O, no clock, no host timezone. All datetimes must be timezone-aware.

A FORM session is a candidate for a Garmin swim when both have the same environment, their
starts differ by at most ``MAX_START_DELTA_S`` seconds and their time ranges overlap.
Assignment is one-to-one: candidate pairs are sorted globally by ``|start delta|`` (ties broken
by FORM session id, then activity id) and accepted greedily, so the result does not depend on
input order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

MAX_START_DELTA_S = 600
_POOL_LENGTH_TOLERANCE_M = 0.01


@dataclass(frozen=True)
class FormSwim:
    form_session_id: str
    start: datetime
    end: datetime
    environment: str
    pool_length_m: float | None = None


@dataclass(frozen=True)
class GarminSwim:
    activity_id: str
    start: datetime
    end: datetime
    environment: str
    pool_length_m: float | None = None


@dataclass(frozen=True)
class FormMatch:
    """Match result for one FORM session.

    ``match_delta_s`` is signed: FORM start minus Garmin start in seconds (positive when the
    FORM session started after the Garmin swim). It is ``None`` when unmatched.
    ``pool_length_mismatch`` is 1 only for a match where both pool lengths are known and
    differ, else 0. ``overlaps_garmin_swim`` is True when the session overlaps any Garmin swim
    in time in any environment (meaningful for unmatched sessions; also True for matched ones).
    """

    form_session_id: str
    matched_activity_id: str | None
    match_delta_s: float | None
    pool_length_mismatch: int
    overlaps_garmin_swim: bool


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _pool_mismatch(form: FormSwim, garmin: GarminSwim) -> int:
    if form.pool_length_m is None or garmin.pool_length_m is None:
        return 0
    return int(abs(form.pool_length_m - garmin.pool_length_m) > _POOL_LENGTH_TOLERANCE_M)


def match_form_sessions(
    form_sessions: Sequence[FormSwim], garmin_swims: Sequence[GarminSwim]
) -> list[FormMatch]:
    """One ``FormMatch`` per FORM session, ordered by ``form_session_id``."""
    candidates: list[tuple[float, str, str, float, FormSwim, GarminSwim]] = []
    for form in form_sessions:
        for garmin in garmin_swims:
            if form.environment != garmin.environment:
                continue
            delta = (form.start - garmin.start).total_seconds()
            if abs(delta) > MAX_START_DELTA_S:
                continue
            if not _overlaps(form.start, form.end, garmin.start, garmin.end):
                continue
            candidates.append(
                (abs(delta), form.form_session_id, garmin.activity_id, delta, form, garmin)
            )
    candidates.sort(key=lambda c: c[:3])

    assigned: dict[str, tuple[float, FormSwim, GarminSwim]] = {}
    taken: set[str] = set()
    for _abs, form_id, activity_id, delta, form, garmin in candidates:
        if form_id in assigned or activity_id in taken:
            continue
        assigned[form_id] = (delta, form, garmin)
        taken.add(activity_id)

    results: list[FormMatch] = []
    for form in sorted(form_sessions, key=lambda f: f.form_session_id):
        hit = assigned.get(form.form_session_id)
        any_overlap = any(_overlaps(form.start, form.end, g.start, g.end) for g in garmin_swims)
        if hit is None:
            results.append(FormMatch(form.form_session_id, None, None, 0, any_overlap))
        else:
            delta, _form, garmin = hit
            results.append(
                FormMatch(
                    form.form_session_id,
                    garmin.activity_id,
                    delta,
                    _pool_mismatch(form, garmin),
                    any_overlap,
                )
            )
    return results
