"""Formula tests against published or hand-checked reference values."""

from datetime import UTC, datetime

import pytest
from astral import Observer
from astral.sun import elevation as astral_elevation

from custom_components.feels_like.const import WIND_MOUNTING_FACTORS
from custom_components.feels_like.formulas import classic, radiant, solar
from custom_components.feels_like.formulas.feels_like import (
    FeelsLikeInputs,
    compute,
)
from custom_components.feels_like.formulas.season import (
    meteorological_season,
)
from custom_components.feels_like.formulas.utci import (
    UTCI_CATEGORIES,
    stress_category,
    utci,
)

# ----------------------------------------------------------------- classic


def test_bom_shade_matches_original_template() -> None:
    """T 10 °C, RH 80 %, 3 m/s wind x 1.5 -> 6.1 °C (the user's YAML template)."""
    assert round(classic.bom_shade(10, 80, 3 * 1.5), 1) == 6.1


def test_bom_vapour_pressure() -> None:
    assert classic.vapour_pressure_bom(10, 80) == pytest.approx(9.808, abs=0.01)
    assert classic.vapour_pressure_bom(10, 150) == classic.vapour_pressure_bom(10, 100)


def test_bom_radiation_with_zero_q() -> None:
    e = classic.vapour_pressure_bom(20, 60)
    assert classic.bom_radiation(20, 60, 3, 0) == pytest.approx(
        20 + 0.348 * e - 0.70 * 3 - 4.25
    )


@pytest.mark.parametrize(
    ("t", "v_kmh", "expected"),
    [(-20, 5, -24), (-20, 30, -33)],  # Environment Canada / Wikipedia check values
)
def test_wind_chill_reference(t: float, v_kmh: float, expected: float) -> None:
    chill = classic.wind_chill_jagti(t, v_kmh / 3.6)
    assert chill is not None
    assert round(chill) == expected


def test_wind_chill_undefined_outside_range() -> None:
    assert classic.wind_chill_jagti(12, 10) is None
    assert classic.wind_chill_jagti(0, 4.0 / 3.6) is None


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def test_heat_index_rothfusz() -> None:
    """90 °F / 50 % -> 94.6 °F (NWS chart: 95)."""
    assert c_to_f(classic.heat_index_nws(f_to_c(90), 50)) == pytest.approx(
        94.6, abs=0.05
    )


def test_heat_index_simple_formula_below_80f() -> None:
    t = f_to_c(70)
    expected = 0.5 * (70 + 61 + (70 - 68) * 1.2 + 50 * 0.094)
    assert c_to_f(classic.heat_index_nws(t, 50)) == pytest.approx(expected)


def test_heat_index_cold_returns_air_temperature() -> None:
    assert classic.heat_index_nws(0, 50) == 0


def test_heat_index_low_humidity_adjustment() -> None:
    t, rh = 100.0, 10.0
    raw = (
        -42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
        - 6.83783e-3 * t * t - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh
        + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh
    )  # fmt: skip
    adj = ((13 - rh) / 4) * ((17 - abs(t - 95)) / 17) ** 0.5
    assert c_to_f(classic.heat_index_nws(f_to_c(t), rh)) == pytest.approx(raw - adj)


def test_heat_index_high_humidity_adjustment() -> None:
    t, rh = 84.0, 95.0
    raw = (
        -42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
        - 6.83783e-3 * t * t - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh
        + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh
    )  # fmt: skip
    adj = ((rh - 85) / 10) * ((87 - t) / 5)
    assert c_to_f(classic.heat_index_nws(f_to_c(t), rh)) == pytest.approx(raw + adj)


@pytest.mark.parametrize(("td", "expected"), [(15, 34), (25, 42)])
def test_humidex_reference(td: float, expected: float) -> None:
    """Environment Canada examples at 30 °C air temperature."""
    assert round(classic.humidex_from_dew_point(30, td)) == expected


def test_humidex_from_rh_equals_dew_point_form() -> None:
    # RH that corresponds to a 15 °C dew point at 30 °C (same e formulation).
    e_td = 6.11 * 2.718281828459045 ** (5417.7530 * (1 / 273.16 - 1 / 288.15))
    e_sat = 6.11 * 2.718281828459045 ** (5417.7530 * (1 / 273.16 - 1 / 303.15))
    rh = 100 * e_td / e_sat
    assert classic.humidex(30, rh) == pytest.approx(
        classic.humidex_from_dew_point(30, 15)
    )


def test_four_season_blend() -> None:
    cold = classic.four_season(10, 80, 4.5)
    assert cold.blend_weight == 0
    assert cold.method_used == "wind_chill"
    assert cold.value == pytest.approx(classic.wind_chill_jagti(10, 4.5))

    warm = classic.four_season(14, 80, 4.5)
    assert warm.blend_weight == 1
    assert warm.value == pytest.approx(classic.bom_shade(14, 80, 4.5))

    mid = classic.four_season(12, 80, 4.5)
    assert mid.blend_weight == pytest.approx(0.5)
    assert cold.value < mid.value < warm.value

    calm = classic.four_season(5, 80, 1.0)
    assert calm.method_used == "air_temperature"
    assert calm.value == 5


# ---------------------------------------------------------- solar / radiant


def test_projected_area_factor() -> None:
    assert radiant.projected_area_factor(45) == pytest.approx(0.2249, abs=1e-4)
    assert radiant.projected_area_factor(0) == 0
    assert radiant.projected_area_factor(-5) == 0


def test_log_wind_factor_matches_jagti_face_level() -> None:
    """Log law over open terrain: 1.5 m -> 10 m is ~1.485, the JAG/TI 1.5 rule."""
    assert radiant.log_wind_factor(1.5) == pytest.approx(1.485, abs=1e-3)


@pytest.mark.parametrize(
    ("mounting", "height"),
    [("mast_10m", 10), ("rooftop_5m", 5), ("pole_3m", 3), ("garden_2m", 2)],
)
def test_wind_mounting_table(mounting: str, height: float) -> None:
    assert WIND_MOUNTING_FACTORS[mounting] == pytest.approx(
        radiant.log_wind_factor(height), abs=0.006
    )


def test_erbs_branches() -> None:
    assert solar.erbs_diffuse_fraction(0.1) == pytest.approx(1 - 0.009)
    assert solar.erbs_diffuse_fraction(0.9) == 0.165
    mid = solar.erbs_diffuse_fraction(0.5)
    assert mid == pytest.approx(
        0.9511 - 0.1604 * 0.5 + 4.388 * 0.25 - 16.638 * 0.125 + 12.336 * 0.0625
    )


def test_erbs_split_clear_day() -> None:
    comps = solar.erbs_split(800, 45, 172)
    assert comps.dhi == pytest.approx(0.165 * 800)
    assert comps.dni == pytest.approx((800 - 132) / 0.7071068, rel=1e-4)


def test_erbs_split_night_and_low_sun() -> None:
    assert solar.erbs_split(0, 30, 100).dni == 0
    assert solar.erbs_split(50, -2, 100).dni == 0
    low = solar.erbs_split(20, 2, 100)  # zenith 88° > 87°
    assert low.dni == 0
    assert low.dhi == 20


def test_erbs_split_caps_beam_for_too_bright_low_sun() -> None:
    """700 W/m² at 5° (e.g. held from midday) would give a ~6700 W/m² beam."""
    comps = solar.erbs_split(700, 5, 172)
    assert comps.dni == pytest.approx(
        solar.SOLAR_CONSTANT * solar.eccentricity_factor(172)
    )


def test_mean_radiant_temperature_full_sun() -> None:
    """Ta 20 °C, GHI 800, sun 45°, albedo 0.2 -> Tmrt ~57.5 °C (hand check)."""
    comps = solar.erbs_split(800, 45, 172)
    load = radiant.shortwave_load(
        ghi=800, dhi=comps.dhi, dni=comps.dni, elevation_deg=45, albedo=0.2,
        include_direct=True,
    )  # fmt: skip
    assert radiant.mean_radiant_temperature(20, load) == pytest.approx(57.5, abs=0.1)
    assert radiant.mean_radiant_temperature(20, 0) == pytest.approx(20)


def test_clear_sky_and_clouds() -> None:
    clear = solar.haurwitz_clear_sky(45)
    assert clear == pytest.approx(
        1098 * 0.7071068 * 2.718281828 ** (-0.059 / 0.7071068)
    )
    assert solar.haurwitz_clear_sky(-1) == 0
    assert solar.kasten_czeplak(clear, 0) == clear
    assert solar.kasten_czeplak(clear, 100) == pytest.approx(0.25 * clear)


def test_lux_conversion() -> None:
    assert solar.lux_to_irradiance(120_000) == pytest.approx(1000)
    assert solar.lux_to_irradiance(-5) == 0


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 6, 21, 11, 0, tzinfo=UTC),
        datetime(2026, 12, 21, 11, 30, tzinfo=UTC),
        datetime(2026, 3, 20, 16, 0, tzinfo=UTC),
    ],
)
def test_astral_elevation_is_sane(when: datetime) -> None:
    """Sanity check the library used for sun position (Bergen, Norway)."""
    elev = astral_elevation(Observer(60.39, 5.32, 0), when, with_refraction=False)
    assert -90 < elev < 90


# --------------------------------------------------------------------- UTCI


def test_utci_reference_value() -> None:
    """pythermalcomfort docs: utci(tdb=25, tr=25, v=1.0, rh=50) -> 24.6."""
    res = utci(25, 50, 1.0, 25)
    assert round(res.value, 1) == 24.6
    assert not res.clamped
    assert stress_category(res.value) == "no_thermal_stress"


def test_utci_clamps_calm_wind() -> None:
    res = utci(10, 80, 0.0, 10)
    assert res.clamped
    assert res.value == pytest.approx(utci(10, 80, 0.5, 10).value)


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (47, "extreme_heat_stress"),
        (40, "very_strong_heat_stress"),
        (35, "strong_heat_stress"),
        (30, "moderate_heat_stress"),
        (20, "no_thermal_stress"),
        (9, "no_thermal_stress"),
        (5, "slight_cold_stress"),
        (-5, "moderate_cold_stress"),
        (-20, "strong_cold_stress"),
        (-30, "very_strong_cold_stress"),
        (-45, "extreme_cold_stress"),
    ],
)
def test_stress_categories(value: float, category: str) -> None:
    assert stress_category(value) == category
    assert category in UTCI_CATEGORIES


# ------------------------------------------------------------------- season


@pytest.mark.parametrize(
    ("month", "lat", "season"),
    [
        (1, 60, "winter"),
        (4, 60, "spring"),
        (7, 60, "summer"),
        (10, 60, "autumn"),
        (1, -33, "summer"),
        (7, -33, "winter"),
        (12, 0, "winter"),
    ],
)
def test_season(month: int, lat: float, season: str) -> None:
    assert meteorological_season(month, lat) == season


# ------------------------------------------------------------------- engine


def test_engine_utci_sun_warmer_than_shade() -> None:
    res = compute(
        FeelsLikeInputs(
            method="utci", temperature=20, humidity=60, wind_10m=3, ghi=800,
            elevation=45, day_of_year=172, albedo=0.2,
        )
    )  # fmt: skip
    assert res.sun is not None
    assert res.sun > res.shade > 15
    assert res.mrt_sun == pytest.approx(57.5, abs=0.1)
    assert res.stress == stress_category(res.sun)


def test_engine_utci_without_sun_uses_air_temperature_as_mrt() -> None:
    res = compute(
        FeelsLikeInputs(method="utci", temperature=10, humidity=80, wind_10m=4.5)
    )
    assert res.sun is None
    assert res.mrt_shade == pytest.approx(10)
    assert res.shade == pytest.approx(utci(10, 80, 4.5, 10).value)


def test_engine_night_sun_equals_shade() -> None:
    res = compute(
        FeelsLikeInputs(
            method="utci", temperature=5, humidity=90, wind_10m=2, ghi=0, elevation=-10
        )
    )
    assert res.sun == pytest.approx(res.shade)


def test_engine_bom_shade_is_template_and_sun_adds_direct_beam() -> None:
    shade_only = compute(
        FeelsLikeInputs(method="bom", temperature=10, humidity=80, wind_10m=4.5)
    )
    assert round(shade_only.shade, 1) == 6.1
    assert shade_only.sun is None

    sunny = compute(
        FeelsLikeInputs(
            method="bom", temperature=10, humidity=80, wind_10m=4.5, ghi=800,
            elevation=45, day_of_year=172,
        )
    )  # fmt: skip
    assert sunny.shade == pytest.approx(shade_only.shade)
    assert sunny.sun is not None
    assert sunny.dni is not None
    expected_q = radiant.ALPHA_IR * radiant.projected_area_factor(45) * sunny.dni
    assert sunny.sun - sunny.shade == pytest.approx(0.70 * expected_q / 14.5)


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("four_season", {"humidity": 80, "wind_10m": 4.5}),
        ("wind_chill", {"wind_10m": 4.5}),
        ("heat_index", {"humidity": 50}),
        ("humidex", {"humidity": 50}),
    ],
)
def test_engine_classic_methods_have_no_sun(method: str, kwargs: dict) -> None:
    res = compute(
        FeelsLikeInputs(method=method, temperature=8, ghi=500, elevation=30, **kwargs)
    )
    assert res.sun is None
    assert res.stress is None


def test_engine_missing_required_input() -> None:
    with pytest.raises(ValueError, match="requires"):
        compute(FeelsLikeInputs(method="utci", temperature=10, humidity=80))
