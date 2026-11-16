"""Solar irradiance helpers.

Solar position itself comes from the astral library (shipped with Home
Assistant); this module only needs the resulting elevation angle.

Sources:
- Earth-sun distance correction: Spencer (1971).
- Clear-sky global irradiance: Haurwitz (1945), constants as implemented in
  pvlib: G = 1098 cos z exp(-0.059 / cos z).
- Cloud attenuation: Kasten & Czeplak (1980), G = G_clear (1 - 0.75 (N/8)^3.4),
  fitted on Hamburg data for solar elevations above 5°.
- Direct/diffuse split: Erbs, Klein & Duffie (1982); limits as in pvlib
  (min cos zenith 0.065, no direct beam beyond 87° zenith).
- Lux to W/m²: ~120 lx per W/m² for daylight (about 122 klx per 1000 W/m²
  measured for outdoor sunlight).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SOLAR_CONSTANT = 1361.0  # W/m²
DEFAULT_LUX_PER_WM2 = 120.0
ERBS_MIN_COS_ZENITH = 0.065
ERBS_MAX_ZENITH = 87.0


def eccentricity_factor(day_of_year: int) -> float:
    """Earth-sun distance correction E0 (Spencer 1971)."""
    g = 2.0 * math.pi * (day_of_year - 1) / 365.0
    return (
        1.000110
        + 0.034221 * math.cos(g)
        + 0.001280 * math.sin(g)
        + 0.000719 * math.cos(2 * g)
        + 0.000077 * math.sin(2 * g)
    )


def extraterrestrial_horizontal(elevation_deg: float, day_of_year: int) -> float:
    """Top-of-atmosphere irradiance on a horizontal surface (W/m²)."""
    sin_elev = math.sin(math.radians(elevation_deg))
    if sin_elev <= 0:
        return 0.0
    return SOLAR_CONSTANT * eccentricity_factor(day_of_year) * sin_elev


def haurwitz_clear_sky(elevation_deg: float) -> float:
    """Clear-sky global horizontal irradiance (W/m²), Haurwitz model."""
    cos_zenith = math.sin(math.radians(elevation_deg))
    if cos_zenith <= 0:
        return 0.0
    return 1098.0 * cos_zenith * math.exp(-0.059 / cos_zenith)


def kasten_czeplak(clear_sky_ghi: float, cloud_cover_pct: float) -> float:
    """Global irradiance under cloud cover given in percent (0-100)."""
    fraction = max(0.0, min(100.0, cloud_cover_pct)) / 100.0  # = N/8 in oktas
    return clear_sky_ghi * (1.0 - 0.75 * fraction**3.4)


def lux_to_irradiance(lux: float, lux_per_wm2: float = DEFAULT_LUX_PER_WM2) -> float:
    """Approximate global irradiance (W/m²) from daylight illuminance (lx)."""
    if lux_per_wm2 <= 0:
        raise ValueError("lux_per_wm2 must be positive")
    return max(lux, 0.0) / lux_per_wm2


def erbs_diffuse_fraction(kt: float) -> float:
    """Erbs et al. (1982) diffuse fraction as a function of clearness index."""
    if kt <= 0.22:
        return 1.0 - 0.09 * kt
    if kt <= 0.80:
        return 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    return 0.165


@dataclass(frozen=True, slots=True)
class IrradianceComponents:
    """Global irradiance split into components (all W/m²)."""

    ghi: float
    """Global horizontal irradiance."""
    dhi: float
    """Diffuse horizontal irradiance."""
    dni: float
    """Direct normal irradiance (on a plane facing the sun)."""
    kt: float
    """Clearness index used for the split."""


def erbs_split(
    ghi: float, elevation_deg: float, day_of_year: int
) -> IrradianceComponents:
    """Split measured or estimated GHI into diffuse and direct components."""
    ghi = max(ghi, 0.0)
    zenith = 90.0 - elevation_deg
    if ghi == 0.0 or zenith >= 90.0:
        return IrradianceComponents(ghi=ghi, dhi=ghi, dni=0.0, kt=0.0)

    cos_zenith = math.cos(math.radians(zenith))
    i0 = SOLAR_CONSTANT * eccentricity_factor(day_of_year)
    i0h = i0 * max(cos_zenith, ERBS_MIN_COS_ZENITH)
    kt = max(0.0, min(1.0, ghi / i0h))
    dhi = erbs_diffuse_fraction(kt) * ghi

    if zenith > ERBS_MAX_ZENITH:
        return IrradianceComponents(ghi=ghi, dhi=ghi, dni=0.0, kt=kt)

    # A GHI too high for the sun's elevation (a wrongly scaled sensor, or a value
    # held from earlier in the day) would give an unphysical beam at low sun;
    # the beam can never exceed the irradiance at the top of the atmosphere.
    dni = min((ghi - dhi) / cos_zenith, i0)
    return IrradianceComponents(ghi=ghi, dhi=dhi, dni=max(dni, 0.0), kt=kt)
