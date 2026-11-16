"""Property-based tests: invariants that must hold across the whole input domain."""

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.feels_like.formulas import classic, solar
from custom_components.feels_like.formulas.feels_like import (
    METHOD_SPECS,
    METHODS,
    FeelsLikeInputs,
    compute,
)
from custom_components.feels_like.formulas.utci import (
    DELTA_MRT_MAX,
    DELTA_MRT_MIN,
    TA_MAX,
    TA_MIN,
    UTCI_CATEGORIES,
    WIND_MAX,
    WIND_MIN,
    stress_category,
    utci,
)

# thermofeel's first call can be slow; the formulas themselves are cheap.
settings.register_profile("formulas", deadline=None, max_examples=200)
settings.load_profile("formulas")

temperature = st.floats(-60, 60, allow_nan=False)
humidity = st.floats(0, 100, allow_nan=False)
wind = st.floats(0, 40, allow_nan=False)
ghi = st.one_of(st.none(), st.floats(0, 1400, allow_nan=False))
elevation = st.floats(-90, 90, allow_nan=False)
day_of_year = st.integers(1, 366)
albedo = st.floats(0, 1, allow_nan=False)


@given(
    method=st.sampled_from(METHODS),
    t=temperature,
    rh=humidity,
    v=wind,
    irradiance=ghi,
    elev=elevation,
    doy=day_of_year,
    alb=albedo,
)
def test_compute_is_finite_for_every_method(
    method: str,
    t: float,
    rh: float,
    v: float,
    irradiance: float | None,
    elev: float,
    doy: int,
    alb: float,
) -> None:
    spec = METHOD_SPECS[method]
    res = compute(
        FeelsLikeInputs(
            method=method,
            temperature=t,
            humidity=rh if spec.needs_humidity else None,
            wind_10m=v if spec.needs_wind else None,
            ghi=irradiance,
            elevation=elev,
            day_of_year=doy,
            albedo=alb,
        )
    )
    assert math.isfinite(res.shade)
    if res.sun is not None:
        assert spec.supports_sun
        assert irradiance is not None
        assert math.isfinite(res.sun)
    if res.stress is not None:
        assert res.stress in UTCI_CATEGORIES


@given(
    t=st.floats(TA_MIN, TA_MAX),
    rh=humidity,
    v=st.floats(WIND_MIN, WIND_MAX),
    delta=st.floats(DELTA_MRT_MIN, DELTA_MRT_MAX),
)
def test_utci_in_range_inputs_are_not_clamped(
    t: float, rh: float, v: float, delta: float
) -> None:
    res = utci(t, rh, v, t + delta)
    assert math.isfinite(res.value)
    # Only the vapour-pressure limit (e <= 50 hPa) can still clamp in range.
    if res.vapour_pressure < 50.0:
        assert not res.clamped


@given(t=temperature, rh=humidity, v=wind, tmrt=st.floats(-80, 120))
def test_utci_is_finite_for_any_input(
    t: float, rh: float, v: float, tmrt: float
) -> None:
    assert math.isfinite(utci(t, rh, v, tmrt).value)


@given(a=st.floats(-100, 100), b=st.floats(-100, 100))
def test_stress_category_is_total_and_monotone(a: float, b: float) -> None:
    low, high = sorted((a, b))
    index = {name: i for i, name in enumerate(UTCI_CATEGORIES)}
    assert index[stress_category(low)] <= index[stress_category(high)]


@given(g=st.floats(-100, 1500), elev=elevation, doy=day_of_year)
def test_erbs_split_components_are_physical(g: float, elev: float, doy: int) -> None:
    comps = solar.erbs_split(g, elev, doy)
    assert comps.ghi >= 0
    assert 0 <= comps.kt <= 1
    assert comps.dni >= 0
    assert -1e-9 <= comps.dhi <= comps.ghi + 1e-9


@given(t=st.floats(-60, 10), v=st.floats(1.4, 40))
def test_wind_chill_never_exceeds_air_temperature(t: float, v: float) -> None:
    chill = classic.wind_chill_jagti(t, v)
    if chill is not None:
        assert chill <= t + 1e-9


@given(
    value=st.floats(-1e6, 1e6),
    low=st.floats(-100, 0),
    high=st.floats(0, 100),
)
def test_clamp_is_bounded_and_idempotent(value: float, low: float, high: float) -> None:
    once = classic.clamp(value, low, high)
    assert low <= once <= high
    assert classic.clamp(once, low, high) == once
