"""Classic "feels like" indices.

Sources:
- BoM Apparent Temperature (Steadman 1994), as published by the Australian
  Bureau of Meteorology: shade form ``Ta + 0.33e - 0.70ws - 4.00`` and radiation
  form ``Ta + 0.348e - 0.70ws + 0.70Q/(ws + 10) - 4.25``; ws is the 10 m wind.
- JAG/TI wind chill (Environment Canada / NWS / Met Office, 2001), defined for
  T <= 10 °C and 10 m wind > 4.8 km/h.
- NWS heat index, NOAA Weather Prediction Center algorithm (Rothfusz 1990 with
  the simple Steadman formula and the low/high humidity adjustments).
- Humidex (Environment Canada).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

WIND_CHILL_MAX_TEMP_C = 10.0
WIND_CHILL_MIN_WIND_KMH = 4.8

FOUR_SEASON_COLD_LIMIT_C = 10.0
FOUR_SEASON_WARM_LIMIT_C = 14.0


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value to the closed interval [low, high]."""
    return max(low, min(high, value))


def vapour_pressure_bom(t_c: float, rh: float) -> float:
    """Water vapour pressure in hPa, as used by the BoM apparent temperature."""
    rh = clamp(rh, 0.0, 100.0)
    return rh / 100.0 * 6.105 * math.exp(17.27 * t_c / (237.7 + t_c))


def bom_shade(t_c: float, rh: float, wind_10m: float) -> float:
    """BoM apparent temperature, shade (no radiation) version."""
    e = vapour_pressure_bom(t_c, rh)
    return t_c + 0.33 * e - 0.70 * wind_10m - 4.00


def bom_radiation(t_c: float, rh: float, wind_10m: float, q: float) -> float:
    """BoM apparent temperature including net absorbed radiation Q (W/m²)."""
    e = vapour_pressure_bom(t_c, rh)
    return t_c + 0.348 * e - 0.70 * wind_10m + 0.70 * q / (wind_10m + 10.0) - 4.25


def bom_sun_increment(wind_10m: float, delta_q: float) -> float:
    """Extra warmth from additional absorbed radiation, BoM radiation term.

    Used to derive a "sun" value from the operational shade formula, so the sun
    value is never colder than the shade value.
    """
    return 0.70 * max(delta_q, 0.0) / (wind_10m + 10.0)


def _wind_chill_formula(t_c: float, v_kmh: float) -> float:
    v016 = v_kmh**0.16
    return 13.12 + 0.6215 * t_c - 11.37 * v016 + 0.3965 * t_c * v016


def wind_chill_jagti(t_c: float, wind_10m: float) -> float | None:
    """JAG/TI wind chill index in °C, or None outside its defined range."""
    v_kmh = wind_10m * 3.6
    if t_c > WIND_CHILL_MAX_TEMP_C or v_kmh <= WIND_CHILL_MIN_WIND_KMH:
        return None
    return _wind_chill_formula(t_c, v_kmh)


def heat_index_nws(t_c: float, rh: float) -> float:
    """NWS heat index in °C (WPC algorithm, computed in °F internally)."""
    rh = clamp(rh, 0.0, 100.0)
    t = t_c * 9.0 / 5.0 + 32.0
    if t <= 40.0:
        return t_c

    # Simple Steadman formula (already averaged with temperature).
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    if hi >= 80.0:
        hi = (
            -42.379
            + 2.04901523 * t
            + 10.14333127 * rh
            - 0.22475541 * t * rh
            - 6.83783e-3 * t * t
            - 5.481717e-2 * rh * rh
            + 1.22874e-3 * t * t * rh
            + 8.5282e-4 * t * rh * rh
            - 1.99e-6 * t * t * rh * rh
        )
        if rh < 13.0 and 80.0 <= t <= 112.0:
            hi -= ((13.0 - rh) / 4.0) * math.sqrt((17.0 - abs(t - 95.0)) / 17.0)
        elif rh > 85.0 and 80.0 <= t <= 87.0:
            hi += ((rh - 85.0) / 10.0) * ((87.0 - t) / 5.0)
    return (hi - 32.0) * 5.0 / 9.0


def vapour_pressure_humidex(t_c: float, rh: float) -> float:
    """Vapour pressure in hPa with the Humidex Clausius-Clapeyron constants.

    e(Td) = 6.11 exp(5417.7530 (1/273.16 - 1/Td)) and e(Td) = RH/100 * e(T), so
    the dew point is not needed when relative humidity is known.
    """
    rh = clamp(rh, 0.0, 100.0)
    t_k = t_c + 273.15
    return rh / 100.0 * 6.11 * math.exp(5417.7530 * (1.0 / 273.16 - 1.0 / t_k))


def humidex(t_c: float, rh: float) -> float:
    """Environment Canada Humidex."""
    return t_c + 0.5555 * (vapour_pressure_humidex(t_c, rh) - 10.0)


def humidex_from_dew_point(t_c: float, td_c: float) -> float:
    """Humidex from air temperature and dew point (the official form)."""
    e = 6.11 * math.exp(5417.7530 * (1.0 / 273.16 - 1.0 / (td_c + 273.15)))
    return t_c + 0.5555 * (e - 10.0)


@dataclass(frozen=True, slots=True)
class FourSeasonResult:
    """Result of the four-season blend."""

    value: float
    blend_weight: float
    """0 = pure cold-weather index, 1 = pure BoM apparent temperature."""
    method_used: str


def four_season(t_c: float, rh: float, wind_10m: float) -> FourSeasonResult:
    """Wind chill in the cold, BoM apparent temperature in the warm.

    Linear blend between 10 °C and 14 °C (the transition band MetService uses).
    Inside the band the wind chill formula is evaluated slightly above its
    nominal 10 °C limit so the blend is continuous. Where wind is too light for
    wind chill (<= 4.8 km/h), the air temperature is used as the cold index.
    """
    weight = clamp(
        (t_c - FOUR_SEASON_COLD_LIMIT_C)
        / (FOUR_SEASON_WARM_LIMIT_C - FOUR_SEASON_COLD_LIMIT_C),
        0.0,
        1.0,
    )
    warm = bom_shade(t_c, rh, wind_10m)
    v_kmh = wind_10m * 3.6
    if v_kmh > WIND_CHILL_MIN_WIND_KMH:
        cold = _wind_chill_formula(min(t_c, FOUR_SEASON_WARM_LIMIT_C), v_kmh)
        cold_method = "wind_chill"
    else:
        cold = t_c
        cold_method = "air_temperature"

    if weight <= 0.0:
        return FourSeasonResult(cold, 0.0, cold_method)
    if weight >= 1.0:
        return FourSeasonResult(warm, 1.0, "bom")
    return FourSeasonResult(
        (1.0 - weight) * cold + weight * warm, weight, f"blend_{cold_method}_bom"
    )
