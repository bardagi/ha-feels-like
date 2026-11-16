"""Universal Thermal Climate Index (UTCI).

The UTCI is the air temperature of a reference environment that would cause the
same physiological response (Fiala multi-node model with an adaptive clothing
model) as the actual conditions. The operational 6th-order polynomial of
Bröde et al. (2012) is evaluated with ECMWF's thermofeel library.

The polynomial is only valid for:
    -50 <= Ta <= 50 °C, 0.5 <= v10 <= 17 m/s, -30 <= Tmrt - Ta <= 70 °C,
    e <= 50 hPa.
thermofeel does not clamp its inputs, so this module does, and reports it.

Stress categories: Błażejczyk et al. (2013), as listed by thermofeel.
"""

from __future__ import annotations

from dataclasses import dataclass

from thermofeel import calculate_saturation_vapour_pressure, calculate_utci

KELVIN = 273.15

TA_MIN, TA_MAX = -50.0, 50.0
WIND_MIN, WIND_MAX = 0.5, 17.0
DELTA_MRT_MIN, DELTA_MRT_MAX = -30.0, 70.0
VAPOUR_PRESSURE_MAX = 50.0  # hPa

UTCI_CATEGORIES: tuple[str, ...] = (
    "extreme_cold_stress",
    "very_strong_cold_stress",
    "strong_cold_stress",
    "moderate_cold_stress",
    "slight_cold_stress",
    "no_thermal_stress",
    "moderate_heat_stress",
    "strong_heat_stress",
    "very_strong_heat_stress",
    "extreme_heat_stress",
)


@dataclass(frozen=True, slots=True)
class UtciResult:
    """UTCI value and diagnostics."""

    value: float
    """UTCI in °C."""
    vapour_pressure: float
    """Water vapour pressure used (hPa)."""
    clamped: bool
    """True if any input was moved into the polynomial's validity range."""


def _clamp(value: float, low: float, high: float) -> tuple[float, bool]:
    clamped = min(max(value, low), high)
    return clamped, clamped != value


def saturation_vapour_pressure(t_c: float) -> float:
    """Saturation vapour pressure over water (hPa), thermofeel formulation."""
    return float(calculate_saturation_vapour_pressure(t_c + KELVIN))


def utci(t_c: float, rh: float, wind_10m: float, tmrt_c: float) -> UtciResult:
    """Compute the UTCI (°C), clamping inputs to the valid range."""
    rh_c, _ = _clamp(rh, 0.0, 100.0)
    ta, c_ta = _clamp(t_c, TA_MIN, TA_MAX)
    va, c_va = _clamp(wind_10m, WIND_MIN, WIND_MAX)
    delta, c_delta = _clamp(tmrt_c - t_c, DELTA_MRT_MIN, DELTA_MRT_MAX)
    e, c_e = _clamp(
        rh_c / 100.0 * saturation_vapour_pressure(ta), 0.0, VAPOUR_PRESSURE_MAX
    )

    t_k = ta + KELVIN
    value = float(calculate_utci(t_k, va, t_k + delta, ehPa=e)) - KELVIN
    return UtciResult(
        value=value, vapour_pressure=e, clamped=c_ta or c_va or c_delta or c_e
    )


def stress_category(utci_c: float) -> str:
    """UTCI assessment scale (10 categories)."""
    if utci_c > 46:
        return "extreme_heat_stress"
    if utci_c > 38:
        return "very_strong_heat_stress"
    if utci_c > 32:
        return "strong_heat_stress"
    if utci_c > 26:
        return "moderate_heat_stress"
    if utci_c >= 9:
        return "no_thermal_stress"
    if utci_c >= 0:
        return "slight_cold_stress"
    if utci_c >= -13:
        return "moderate_cold_stress"
    if utci_c >= -27:
        return "strong_cold_stress"
    if utci_c >= -40:
        return "very_strong_cold_stress"
    return "extreme_cold_stress"
