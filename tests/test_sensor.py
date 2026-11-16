"""Sensor and coordinator tests."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.feels_like.const import DOMAIN
from custom_components.feels_like.formulas import classic
from custom_components.feels_like.formulas.utci import utci

NOON_JUNE = "2026-06-21 11:00:00+00:00"  # ~13:00 local in Bergen
MIDNIGHT_JUNE = "2026-06-21 23:00:00+00:00"

UTCI_OPTIONS = {
    "name": "Ute",
    "method": "utci",
    "temperature_entity": "sensor.temp",
    "humidity_entity": "sensor.hum",
    "wind_entity": "sensor.wind",
    "wind_mounting": "face_level",
    "sun_source": "irradiance",
    "irradiance_entity": "sensor.solar",
    "ground_surface": "grass",
}


@pytest.fixture(autouse=True)
async def bergen(hass: HomeAssistant) -> None:
    """Place Home Assistant in Bergen, Norway."""
    hass.config.latitude = 60.39
    hass.config.longitude = 5.32
    await hass.config.async_set_time_zone("Europe/Oslo")


def _set_inputs(hass: HomeAssistant, **overrides: Any) -> None:
    values = {
        "sensor.temp": ("10", "°C"),
        "sensor.hum": ("80", "%"),
        "sensor.wind": ("3", "m/s"),
        "sensor.solar": ("700", "W/m²"),
    }
    values.update(overrides)
    for entity_id, (state, unit) in values.items():
        hass.states.async_set(entity_id, state, {"unit_of_measurement": unit})


async def _setup(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=options["name"], options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _float(hass: HomeAssistant, entity_id: str) -> float:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return float(state.state)


async def test_utci_creates_sun_shade_and_stress(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass)
    await _setup(hass, UTCI_OPTIONS)

    sun = hass.states.get("sensor.ute_sun")
    shade = hass.states.get("sensor.ute_shade")
    stress = hass.states.get("sensor.ute_thermal_stress")
    assert sun is not None and shade is not None and stress is not None

    assert float(sun.state) > float(shade.state)
    assert sun.attributes["unit_of_measurement"] == "°C"
    assert sun.attributes["device_class"] == "temperature"
    assert sun.attributes["irradiance_source"] == "sensor"
    assert sun.attributes["season"] == "summer"
    assert sun.attributes["solar_elevation"] > 45
    assert shade.attributes["method"] == "utci"
    assert shade.attributes["wind_speed_10m"] == pytest.approx(4.5)
    assert stress.state in stress.attributes["options"]
    assert stress.attributes["based_on"] == "sun"
    assert stress.attributes["utci"] == pytest.approx(float(sun.state), abs=0.01)


async def test_unit_conversion(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """°F and km/h inputs give the same result as °C and m/s."""
    freezer.move_to(MIDNIGHT_JUNE)
    _set_inputs(hass, **{"sensor.temp": ("50", "°F"), "sensor.wind": ("10.8", "km/h")})
    await _setup(hass, {**UTCI_OPTIONS, "sun_source": "none"})

    expected = utci(10, 80, 4.5, 10).value
    assert _float(hass, "sensor.ute") == pytest.approx(expected, abs=0.01)


async def test_unavailable_input_and_recovery(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass)
    await _setup(hass, UTCI_OPTIONS)
    assert hass.states.get("sensor.ute_shade").state != STATE_UNAVAILABLE

    hass.states.async_set("sensor.temp", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    for entity_id in (
        "sensor.ute_sun",
        "sensor.ute_shade",
        "sensor.ute_thermal_stress",
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    hass.states.async_set("sensor.temp", "12", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ute_shade").state != STATE_UNAVAILABLE


async def test_sun_sensor_unavailable_when_sun_input_missing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass)
    await _setup(hass, UTCI_OPTIONS)

    hass.states.async_set("sensor.solar", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ute_sun").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.ute_shade").state != STATE_UNAVAILABLE
    assert (
        hass.states.get("sensor.ute_thermal_stress").attributes["based_on"] == "shade"
    )


async def test_night_sun_equals_shade(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MIDNIGHT_JUNE)
    _set_inputs(hass, **{"sensor.solar": ("0", "W/m²")})
    await _setup(hass, UTCI_OPTIONS)
    assert _float(hass, "sensor.ute_sun") == pytest.approx(
        _float(hass, "sensor.ute_shade")
    )


async def test_bom_without_sun_matches_template(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass)
    options = {
        "name": "Apparent Temperature Ute",
        "method": "bom",
        "temperature_entity": "sensor.temp",
        "humidity_entity": "sensor.hum",
        "wind_entity": "sensor.wind",
        "wind_mounting": "face_level",
        "sun_source": "none",
    }
    await _setup(hass, options)
    state = hass.states.get("sensor.apparent_temperature_ute")
    assert state is not None
    assert round(float(state.state), 1) == 6.1
    assert hass.states.get("sensor.apparent_temperature_ute_sun") is None
    assert hass.states.get("sensor.apparent_temperature_ute_thermal_stress") is None


async def test_weather_cloud_cover_estimate(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass)
    hass.states.async_set("weather.home", "sunny", {"cloud_coverage": 0})
    await _setup(
        hass,
        {
            **UTCI_OPTIONS,
            "sun_source": "weather",
            "weather_entity": "weather.home",
        },
    )
    clear = hass.states.get("sensor.ute_sun").attributes["irradiance"]
    assert (
        hass.states.get("sensor.ute_sun").attributes["irradiance_source"] == "estimated"
    )
    assert clear > 600

    hass.states.async_set("weather.home", "cloudy", {"cloud_coverage": 100})
    await hass.async_block_till_done()
    overcast = hass.states.get("sensor.ute_sun").attributes["irradiance"]
    assert overcast == pytest.approx(0.25 * clear, rel=0.01)


async def test_illuminance_and_snow(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass, **{"sensor.lux": ("84000", "lx")})
    hass.states.async_set("input_boolean.snow", "off")
    await _setup(
        hass,
        {
            **UTCI_OPTIONS,
            "sun_source": "illuminance",
            "illuminance_entity": "sensor.lux",
            "lux_per_wm2": 120,
            "snow_entity": "input_boolean.snow",
        },
    )
    sun = hass.states.get("sensor.ute_sun")
    assert sun.attributes["irradiance"] == pytest.approx(700)
    assert sun.attributes["albedo"] == 0.2
    no_snow = float(sun.state)

    hass.states.async_set("input_boolean.snow", "on")
    await hass.async_block_till_done()
    sun = hass.states.get("sensor.ute_sun")
    assert sun.attributes["albedo"] == 0.7
    assert float(sun.state) > no_snow


async def test_four_season_attributes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass, **{"sensor.temp": ("12", "°C")})
    await _setup(
        hass,
        {
            "name": "Ute",
            "method": "four_season",
            "temperature_entity": "sensor.temp",
            "humidity_entity": "sensor.hum",
            "wind_entity": "sensor.wind",
            "wind_mounting": "face_level",
        },
    )
    state = hass.states.get("sensor.ute")
    assert state.attributes["method_used"] == "blend_wind_chill_bom"
    assert state.attributes["blend_weight"] == 0.5
    assert float(state.state) == pytest.approx(
        classic.four_season(12, 80, 4.5).value, abs=0.01
    )


async def test_periodic_refresh_follows_the_sun(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-06-21 05:00:00+00:00")
    _set_inputs(hass)
    await _setup(hass, UTCI_OPTIONS)
    before = hass.states.get("sensor.ute_sun").attributes["solar_elevation"]

    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    after = hass.states.get("sensor.ute_sun").attributes["solar_elevation"]
    assert after > before


async def test_unload(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass)
    entry = await _setup(hass, UTCI_OPTIONS)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ute_shade").state == STATE_UNAVAILABLE


async def test_config_problem_warns_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wrong unit is a configuration problem: warn once, not on every update."""
    freezer.move_to(NOON_JUNE)
    _set_inputs(hass, **{"sensor.temp": ("10", "W")})
    await _setup(hass, UTCI_OPTIONS)
    assert hass.states.get("sensor.ute_shade").state == STATE_UNAVAILABLE

    hass.states.async_set("sensor.hum", "81", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    warnings = [r for r in caplog.records if "unsupported unit" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"

    hass.states.async_set("sensor.temp", "10", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ute_shade").state != STATE_UNAVAILABLE
    assert any(
        "available again" in r.getMessage() and r.levelname == "INFO"
        for r in caplog.records
    )
