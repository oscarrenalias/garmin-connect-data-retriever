"""Garmin ``typeKey`` to sport family mapping (SPEC §7)."""

from __future__ import annotations

SPORTS_VERSION = "4"

OTHER_FAMILY = "other"

_FAMILY_TYPE_KEYS: dict[str, tuple[str, ...]] = {
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

SPORT_FAMILIES: dict[str, str] = {
    type_key: family for family, keys in _FAMILY_TYPE_KEYS.items() for type_key in keys
}


def sport_family(type_key: str | None) -> str:
    """Return the family for a Garmin ``typeKey``; ``'other'`` if unlisted or missing."""
    if type_key is None:
        return OTHER_FAMILY
    return SPORT_FAMILIES.get(type_key, OTHER_FAMILY)
