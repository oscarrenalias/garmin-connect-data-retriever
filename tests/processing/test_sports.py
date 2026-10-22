from __future__ import annotations

import pytest

from garmin_archive.processing import sports
from garmin_archive.processing.sports import SPORTS_VERSION, sport_family

SPEC_TABLE = {
    "running": (
        "running",
        "treadmill_running",
        "trail_running",
        "track_running",
        "street_running",
        "indoor_running",
        "virtual_run",
    ),
    "swimming": ("lap_swimming", "open_water_swimming"),
    "hiit": ("hiit",),
    "cardio": ("indoor_cardio",),
    "cycling": (
        "cycling",
        "road_biking",
        "mountain_biking",
        "gravel_cycling",
        "indoor_cycling",
        "virtual_ride",
    ),
    "walking": ("walking", "casual_walking", "speed_walking"),
}


@pytest.mark.parametrize(
    ("type_key", "family"),
    [(key, family) for family, keys in SPEC_TABLE.items() for key in keys],
)
def test_every_spec_key_maps_to_its_family(type_key: str, family: str) -> None:
    assert sport_family(type_key) == family


@pytest.mark.parametrize(
    "type_key", ["mobility", "strength_training", "synthetic_unknown_sport", "", None, "Running"]
)
def test_other_keys_map_to_other(type_key: str | None) -> None:
    assert sport_family(type_key) == "other"


def test_mapping_has_no_extra_keys() -> None:
    assert set(sports.SPORT_FAMILIES) == {k for keys in SPEC_TABLE.values() for k in keys}


def test_sports_version_is_non_empty_string() -> None:
    assert isinstance(SPORTS_VERSION, str)
    assert SPORTS_VERSION
