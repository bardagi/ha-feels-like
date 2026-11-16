"""Feels-like temperature engine: combines inputs, radiation and a method."""

from __future__ import annotations

from dataclasses import dataclass

from . import classic, radiant, solar
from .utci import stress_category, utci

METHOD_UTCI = "utci"
METHOD_BOM = "bom"
METHOD_FOUR_SEASON = "four_season"
METHOD_WIND_CHILL = "wind_chill"
METHOD_HEAT_INDEX = "heat_index"
METHOD_HUMIDEX = "humidex"

METHODS: tuple[str, ...] = (
    METHOD_UTCI,
    METHOD_BOM,
    METHOD_FOUR_SEASON,
    METHOD_WIND_CHILL,
    METHOD_HEAT_INDEX,
    METHOD_HUMIDEX,
)


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """Which inputs a method needs and what it can output."""

    needs_humidity: bool
    needs_wind: bool
    supports_sun: bool
    has_stress_category: bool


METHOD_SPECS: dict[str, MethodSpec] = {
    METHOD_UTCI: MethodSpec(True, True, True, True),
    METHOD_BOM: MethodSpec(True, True, True, False),
    METHOD_FOUR_SEASON: MethodSpec(True, True, False, False),
    METHOD_WIND_CHILL: MethodSpec(False, True, False, False),
    METHOD_HEAT_INDEX: MethodSpec(True, False, False, False),
    METHOD_HUMIDEX: MethodSpec(True, False, False, False),
}


@dataclass(frozen=True, slots=True)
class FeelsLikeInputs:
    """Normalised inputs (°C, %, m/s at 10 m, W/m², degrees)."""

    method: str
    temperature: float
    humidity: float | None = None
    wind_10m: float | None = None
    ghi: float | None = None
    """Global horizontal irradiance; None when no sun data is available."""
    elevation: float = 0.0
    """Solar elevation in degrees."""
    day_of_year: int = 1
    albedo: float = 0.2


@dataclass(frozen=True, slots=True)
class FeelsLikeResult:
    """Computed values and diagnostics."""

    shade: float
    sun: float | None
    stress: str | None
    method_used: str
    mrt_shade: float | None = None
    mrt_sun: float | None = None
    vapour_pressure: float | None = None
    blend_weight: float | None = None
    clamped: bool = False
    dhi: float | None = None
    dni: float | None = None


def _require(value: float | None, name: str, method: str) -> float:
    if value is None:
        raise ValueError(f"method {method} requires {name}")
    return value


def compute(inp: FeelsLikeInputs) -> FeelsLikeResult:
    """Compute feels-like temperatures for the configured method."""
    spec = METHOD_SPECS[inp.method]
    t = inp.temperature
    rh = _require(inp.humidity, "humidity", inp.method) if spec.needs_humidity else 0.0
    wind = _require(inp.wind_10m, "wind", inp.method) if spec.needs_wind else 0.0

    if inp.method == METHOD_UTCI:
        return _compute_utci(inp, t, rh, wind)
    if inp.method == METHOD_BOM:
        return _compute_bom(inp, t, rh, wind)
    if inp.method == METHOD_FOUR_SEASON:
        res = classic.four_season(t, rh, wind)
        return FeelsLikeResult(
            shade=res.value,
            sun=None,
            stress=None,
            method_used=res.method_used,
            blend_weight=res.blend_weight,
            vapour_pressure=classic.vapour_pressure_bom(t, rh),
        )
    if inp.method == METHOD_WIND_CHILL:
        chill = classic.wind_chill_jagti(t, wind)
        return FeelsLikeResult(
            shade=chill if chill is not None else t,
            sun=None,
            stress=None,
            method_used="wind_chill" if chill is not None else "air_temperature",
        )
    if inp.method == METHOD_HEAT_INDEX:
        return FeelsLikeResult(
            shade=classic.heat_index_nws(t, rh),
            sun=None,
            stress=None,
            method_used="heat_index",
        )
    if inp.method == METHOD_HUMIDEX:
        return FeelsLikeResult(
            shade=classic.humidex(t, rh),
            sun=None,
            stress=None,
            method_used="humidex",
            vapour_pressure=classic.vapour_pressure_humidex(t, rh),
        )
    raise ValueError(f"unknown method {inp.method}")


def _radiation(
    inp: FeelsLikeInputs,
) -> tuple[solar.IrradianceComponents | None, float, float]:
    """Return irradiance components and short-wave loads (shade, sun)."""
    if inp.ghi is None:
        return None, 0.0, 0.0
    comps = solar.erbs_split(inp.ghi, inp.elevation, inp.day_of_year)
    common = {
        "ghi": comps.ghi,
        "dhi": comps.dhi,
        "dni": comps.dni,
        "elevation_deg": inp.elevation,
        "albedo": inp.albedo,
    }
    shade = radiant.shortwave_load(**common, include_direct=False)
    sun = radiant.shortwave_load(**common, include_direct=True)
    return comps, shade, sun


def _compute_utci(
    inp: FeelsLikeInputs, t: float, rh: float, wind: float
) -> FeelsLikeResult:
    comps, load_shade, load_sun = _radiation(inp)
    mrt_shade = radiant.mean_radiant_temperature(t, load_shade)
    shade = utci(t, rh, wind, mrt_shade)

    sun = None
    mrt_sun = None
    if comps is not None:
        mrt_sun = radiant.mean_radiant_temperature(t, load_sun)
        sun = utci(t, rh, wind, mrt_sun)

    basis = sun if sun is not None else shade
    return FeelsLikeResult(
        shade=shade.value,
        sun=sun.value if sun is not None else None,
        stress=stress_category(basis.value),
        method_used="utci",
        mrt_shade=mrt_shade,
        mrt_sun=mrt_sun,
        vapour_pressure=shade.vapour_pressure,
        clamped=shade.clamped or (sun.clamped if sun is not None else False),
        dhi=comps.dhi if comps else None,
        dni=comps.dni if comps else None,
    )


def _compute_bom(
    inp: FeelsLikeInputs, t: float, rh: float, wind: float
) -> FeelsLikeResult:
    shade = classic.bom_shade(t, rh, wind)
    comps, load_shade, load_sun = _radiation(inp)
    sun = None
    if comps is not None:
        # Extra radiation absorbed when stepping from shade into the sun
        # (the direct beam), added with the BoM radiation term.
        delta_q = radiant.absorbed_shortwave(load_sun - load_shade)
        sun = shade + classic.bom_sun_increment(wind, delta_q)
    return FeelsLikeResult(
        shade=shade,
        sun=sun,
        stress=None,
        method_used="bom",
        vapour_pressure=classic.vapour_pressure_bom(t, rh),
        dhi=comps.dhi if comps else None,
        dni=comps.dni if comps else None,
    )
