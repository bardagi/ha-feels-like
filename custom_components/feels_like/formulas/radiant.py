"""Radiation absorbed by a standing person and mean radiant temperature.

Source: Di Napoli et al. (2020), "Mean radiant temperature from global-scale
numerical weather prediction models", Int J Biometeorol 64 (the method behind
ECMWF's thermofeel), Eq. 14-15:

    Tmrt^4 = (1/σ) [f_a L_dn + f_a L_up
                    + (α_ir/ε_p)(f_a S_diffuse + f_a S_up + f_p I*)]

with f_a = 0.5, α_ir = 0.7, ε_p = 0.97 and the projected area factor
f_p = 0.308 cos(γ (0.998 - γ²/50000)) for solar elevation γ in degrees.

Simplification: a weather station does not measure long-wave radiation, so
the surroundings are assumed to radiate at air temperature
(f_a L_dn + f_a L_up = σ Ta^4). Without sun this gives Tmrt = Ta, the usual
"shade" assumption for UTCI.

Wind height correction uses the neutral logarithmic wind profile over open
terrain (Davenport class 3, z0 = 0.03 m), the standard exposure for 10 m
synoptic wind observations.
"""

from __future__ import annotations

import math

STEFAN_BOLTZMANN = 5.67e-8  # W m-2 K-4
ALPHA_IR = 0.7  # short-wave absorption coefficient of the clothed body
EPSILON_P = 0.97  # emissivity of the clothed body
F_A = 0.5  # angle factor for sky (upper) and ground (lower) hemispheres
OPEN_TERRAIN_Z0 = 0.03  # m
REFERENCE_WIND_HEIGHT = 10.0  # m
KELVIN = 273.15


def projected_area_factor(elevation_deg: float) -> float:
    """Fraction of the body surface projected towards the sun (standing person)."""
    if elevation_deg <= 0:
        return 0.0
    g = elevation_deg
    return 0.308 * math.cos(math.radians(g * (0.998 - g * g / 50000.0)))


def shortwave_load(
    *,
    ghi: float,
    dhi: float,
    dni: float,
    elevation_deg: float,
    albedo: float,
    include_direct: bool,
) -> float:
    """Short-wave radiation incident on the body, weighted by angle factors (W/m²).

    Diffuse sky radiation and ground-reflected radiation reach the body from the
    upper and lower hemispheres; the direct beam only when standing in the sun.
    """
    load = F_A * max(dhi, 0.0) + F_A * albedo * max(ghi, 0.0)
    if include_direct:
        load += projected_area_factor(elevation_deg) * max(dni, 0.0)
    return load


def mean_radiant_temperature(t_c: float, shortwave: float) -> float:
    """Mean radiant temperature (°C) for air temperature and short-wave load."""
    t_k = t_c + KELVIN
    tmrt4 = t_k**4 + ALPHA_IR / (EPSILON_P * STEFAN_BOLTZMANN) * max(shortwave, 0.0)
    return tmrt4**0.25 - KELVIN


def absorbed_shortwave(shortwave: float) -> float:
    """Short-wave radiation absorbed per unit body surface (W/m²)."""
    return ALPHA_IR * max(shortwave, 0.0)


def log_wind_factor(
    sensor_height: float,
    z0: float = OPEN_TERRAIN_Z0,
    reference_height: float = REFERENCE_WIND_HEIGHT,
) -> float:
    """Factor converting wind at sensor height to the 10 m reference height."""
    if sensor_height <= z0:
        raise ValueError("sensor height must be above the roughness length")
    return math.log(reference_height / z0) / math.log(sensor_height / z0)
