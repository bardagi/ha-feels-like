"""Config and options flow tests."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.feels_like.const import DOMAIN

SETUP_PATH = "custom_components.feels_like.async_setup_entry"


@pytest.fixture(autouse=True)
def input_states(hass: HomeAssistant) -> None:
    """Typical weather station entities."""
    hass.states.async_set("sensor.temp", "10", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.hum", "80", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.wind", "3", {"unit_of_measurement": "m/s"})
    hass.states.async_set("sensor.solar", "450", {"unit_of_measurement": "W/m²"})
    hass.states.async_set("sensor.lux", "50000", {"unit_of_measurement": "lx"})
    hass.states.async_set("sensor.power", "5", {"unit_of_measurement": "W"})
    hass.states.async_set("weather.home", "cloudy", {"cloud_coverage": 75})
    hass.states.async_set("weather.basic", "sunny", {"temperature": 10})


def _fields(result) -> set[str]:
    return {str(key) for key in result["data_schema"].schema}


async def _start(hass: HomeAssistant, name: str = "Ute", method: str = "utci"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert _fields(result) == {"name", "method"}
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": name, "method": method}
    )


async def test_utci_with_irradiance(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["step_id"] == "sensors"
    assert _fields(result) == {
        "temperature_entity",
        "humidity_entity",
        "wind_entity",
        "wind_mounting",
        "sun_source",
        "min_update_interval",
        "precision",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "temperature_entity": "sensor.temp",
            "humidity_entity": "sensor.hum",
            "wind_entity": "sensor.wind",
            "wind_mounting": "face_level",
            "sun_source": "irradiance",
        },
    )
    assert result["step_id"] == "sun"
    assert _fields(result) == {"irradiance_entity", "ground_surface", "snow_entity"}

    with patch(SETUP_PATH, return_value=True) as mock_setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"irradiance_entity": "sensor.solar", "ground_surface": "grass"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ute"
    assert result["options"] == {
        "name": "Ute",
        "method": "utci",
        "temperature_entity": "sensor.temp",
        "humidity_entity": "sensor.hum",
        "wind_entity": "sensor.wind",
        "wind_mounting": "face_level",
        "sun_source": "irradiance",
        "min_update_interval": "60",
        "precision": "0",
        "irradiance_entity": "sensor.solar",
        "ground_surface": "grass",
    }
    assert len(mock_setup.mock_calls) == 1


async def test_heat_index_needs_only_temperature_and_humidity(
    hass: HomeAssistant,
) -> None:
    result = await _start(hass, method="heat_index")
    assert _fields(result) == {
        "temperature_entity",
        "humidity_entity",
        "min_update_interval",
        "precision",
    }
    with patch(SETUP_PATH, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"temperature_entity": "sensor.temp", "humidity_entity": "sensor.hum"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_wind_chill_without_humidity(hass: HomeAssistant) -> None:
    result = await _start(hass, method="wind_chill")
    assert _fields(result) == {
        "temperature_entity",
        "wind_entity",
        "wind_mounting",
        "min_update_interval",
        "precision",
    }


async def test_custom_wind_factor_step(hass: HomeAssistant) -> None:
    result = await _start(hass, method="bom")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "temperature_entity": "sensor.temp",
            "humidity_entity": "sensor.hum",
            "wind_entity": "sensor.wind",
            "wind_mounting": "custom",
            "sun_source": "none",
        },
    )
    assert result["step_id"] == "custom_wind"
    with patch(SETUP_PATH, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"wind_factor": 1.2}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["wind_factor"] == 1.2
    assert "irradiance_entity" not in result["options"]


async def test_default_sun_source_is_weather_when_available(
    hass: HomeAssistant,
) -> None:
    result = await _start(hass)
    for key in result["data_schema"].schema:
        if str(key) == "sun_source":
            assert key.default() == "weather"


async def test_weather_and_illuminance_steps(hass: HomeAssistant) -> None:
    result = await _start(hass)
    sensors = {
        "temperature_entity": "sensor.temp",
        "humidity_entity": "sensor.hum",
        "wind_entity": "sensor.wind",
        "wind_mounting": "garden_2m",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**sensors, "sun_source": "weather"}
    )
    assert _fields(result) == {"weather_entity", "ground_surface", "snow_entity"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"weather_entity": "weather.basic", "ground_surface": "grass"},
    )
    assert result["errors"] == {"base": "weather_cloud_coverage"}

    with patch(SETUP_PATH, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"weather_entity": "weather.home", "ground_surface": "snow"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await _start(hass, name="Lux")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**sensors, "sun_source": "illuminance"}
    )
    assert _fields(result) == {
        "illuminance_entity",
        "lux_per_wm2",
        "ground_surface",
        "snow_entity",
    }


@pytest.mark.parametrize(
    ("field", "entity", "error"),
    [
        ("temperature_entity", "sensor.power", "temperature_unit"),
        ("humidity_entity", "sensor.temp", "humidity_unit"),
        ("wind_entity", "sensor.temp", "wind_unit"),
    ],
)
async def test_unit_validation(
    hass: HomeAssistant, field: str, entity: str, error: str
) -> None:
    result = await _start(hass)
    user_input = {
        "temperature_entity": "sensor.temp",
        "humidity_entity": "sensor.hum",
        "wind_entity": "sensor.wind",
        "wind_mounting": "face_level",
        "sun_source": "none",
        field: entity,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_options_flow_switch_method_drops_unused_settings(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ute",
        options={
            "name": "Ute",
            "method": "utci",
            "temperature_entity": "sensor.temp",
            "humidity_entity": "sensor.hum",
            "wind_entity": "sensor.wind",
            "wind_mounting": "face_level",
            "sun_source": "irradiance",
            "irradiance_entity": "sensor.solar",
            "ground_surface": "grass",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert _fields(result) == {"method"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"method": "heat_index"}
    )
    assert result["step_id"] == "sensors"
    with patch(SETUP_PATH, return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"temperature_entity": "sensor.temp", "humidity_entity": "sensor.hum"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "name": "Ute",
        "method": "heat_index",
        "temperature_entity": "sensor.temp",
        "humidity_entity": "sensor.hum",
        "min_update_interval": "60",
        "precision": "0",
    }
