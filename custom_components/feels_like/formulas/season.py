"""Meteorological season from date and latitude."""

from __future__ import annotations

SEASONS: tuple[str, ...] = ("winter", "spring", "summer", "autumn")

# Northern hemisphere meteorological seasons: DJF, MAM, JJA, SON.
_NORTH = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}  # fmt: skip
_OPPOSITE = {
    "winter": "summer",
    "summer": "winter",
    "spring": "autumn",
    "autumn": "spring",
}


def meteorological_season(month: int, latitude: float) -> str:
    """Return winter/spring/summer/autumn for the hemisphere of ``latitude``."""
    if not 1 <= month <= 12:
        raise ValueError("month must be 1-12")
    season = _NORTH[month]
    return season if latitude >= 0 else _OPPOSITE[season]
