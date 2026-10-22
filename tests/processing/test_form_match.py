from __future__ import annotations

from datetime import UTC, datetime, timedelta

from garmin_archive.processing.form_match import FormSwim, GarminSwim, match_form_sessions

T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def form(sid: str, offset_s: float, env: str = "pool", pool: float | None = 25.0, dur: int = 1800):
    start = T0 + timedelta(seconds=offset_s)
    return FormSwim(sid, start, start + timedelta(seconds=dur), env, pool)


def garmin(aid: str, env: str = "pool", pool: float | None = 25.0, dur: int = 1800):
    return GarminSwim(aid, T0, T0 + timedelta(seconds=dur), env, pool)


def test_eight_seconds_matches() -> None:
    [m] = match_form_sessions([form("f1", 8)], [garmin("a1")])
    assert m.matched_activity_id == "a1"
    assert m.match_delta_s == 8
    assert m.pool_length_mismatch == 0


def test_eleven_minutes_does_not_match() -> None:
    [m] = match_form_sessions([form("f1", 660)], [garmin("a1")])
    assert m.matched_activity_id is None
    assert m.match_delta_s is None
    assert m.overlaps_garmin_swim is True


def test_closest_of_two_wins() -> None:
    results = match_form_sessions([form("f1", 100), form("f2", 20)], [garmin("a1")])
    by_id = {m.form_session_id: m for m in results}
    assert by_id["f2"].matched_activity_id == "a1"
    assert by_id["f1"].matched_activity_id is None
    assert by_id["f1"].overlaps_garmin_swim is True


def test_pool_length_mismatch() -> None:
    [m] = match_form_sessions([form("f1", 5, pool=50.0)], [garmin("a1", pool=25.0)])
    assert m.matched_activity_id == "a1"
    assert m.pool_length_mismatch == 1


def test_different_environment_no_match_but_overlap() -> None:
    [m] = match_form_sessions([form("f1", 5, env="open_water")], [garmin("a1")])
    assert m.matched_activity_id is None
    assert m.overlaps_garmin_swim is True


def test_no_overlap_in_time() -> None:
    f = form("f1", 0, dur=60)
    g = GarminSwim("a1", T0 + timedelta(seconds=60), T0 + timedelta(seconds=600), "pool", 25.0)
    [m] = match_form_sessions([f], [g])
    assert m.matched_activity_id is None
    assert m.overlaps_garmin_swim is False


def test_input_order_independent_and_ties_deterministic() -> None:
    forms = [form("f1", 30), form("f2", -30)]
    garmins = [
        garmin("a1"),
        GarminSwim("a2", T0 + timedelta(seconds=10), T0 + timedelta(hours=1), "pool", 25.0),
    ]
    first = match_form_sessions(forms, garmins)
    second = match_form_sessions(forms[::-1], garmins[::-1])
    assert first == second
