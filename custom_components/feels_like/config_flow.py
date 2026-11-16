"""Config and options flow for Feels Like."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import voluptuous as vol
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    LIGHT_LUX,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfIrradiance,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaConfigFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaOptionsFlowHandler,
)
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    CONF_GROUND_SURFACE,
    CONF_HUMIDITY_ENTITY,
    CONF_ILLUMINANCE_ENTITY,
    CONF_IRRADIANCE_ENTITY,
    CONF_LUX_PER_WM2,
    CONF_METHOD,
    CONF_SNOW_ENTITY,
    CONF_SUN_SOURCE,
    CONF_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_WIND_ENTITY,
    CONF_WIND_FACTOR,
    CONF_WIND_MOUNTING,
    DEFAULT_GROUND_SURFACE,
    DEFAULT_LUX_PER_WM2,
    DEFAULT_METHOD,
    DEFAULT_NAME,
    DEFAULT_WIND_MOUNTING,
    DOMAIN,
    GROUND_ALBEDO,
    METHOD_SPECS,
    METHODS,
    SUN_SOURCE_ENTITY_KEYS,
    SUN_SOURCE_ILLUMINANCE,
    SUN_SOURCE_IRRADIANCE,
    SUN_SOURCE_NONE,
    SUN_SOURCE_WEATHER,
    SUN_SOURCES,
    WIND_MOUNTING_CUSTOM,
    WIND_MOUNTING_FACTORS,
    WIND_MOUNTINGS,
)

_SUN_KEYS = (
    CONF_SUN_SOURCE,
    CONF_IRRADIANCE_ENTITY,
    CONF_ILLUMINANCE_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_LUX_PER_WM2,
    CONF_GROUND_SURFACE,
    CONF_SNOW_ENTITY,
)
_IRRADIANCE_UNITS = (
    UnitOfIrradiance.WATTS_PER_SQUARE_METER,
    UnitOfIrradiance.BTUS_PER_HOUR_SQUARE_FOOT,
)


def _sensor_selector() -> selector.EntitySelector:
    # Filter on domain only: many filtered/template sensors lack a device class.
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _select(
    options: list[str], key: str, *, list_mode: bool = False
) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            translation_key=key,
            mode=(
                selector.SelectSelectorMode.LIST
                if list_mode
                else selector.SelectSelectorMode.DROPDOWN
            ),
        )
    )


def _hass(handler: SchemaCommonFlowHandler) -> HomeAssistant:
    return handler.parent_handler.hass


# --------------------------------------------------------------------- method


async def _method_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    schema: dict[Any, Any] = {}
    if not isinstance(handler.parent_handler, SchemaOptionsFlowHandler):
        schema[vol.Required(CONF_NAME, default=DEFAULT_NAME)] = selector.TextSelector()
    schema[vol.Required(CONF_METHOD, default=DEFAULT_METHOD)] = _select(
        list(METHODS), CONF_METHOD, list_mode=True
    )
    return vol.Schema(schema)


# -------------------------------------------------------------------- sensors


async def _sensors_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    spec = METHOD_SPECS[handler.options[CONF_METHOD]]
    schema: dict[Any, Any] = {vol.Required(CONF_TEMPERATURE_ENTITY): _sensor_selector()}
    if spec.needs_humidity:
        schema[vol.Required(CONF_HUMIDITY_ENTITY)] = _sensor_selector()
    if spec.needs_wind:
        schema[vol.Required(CONF_WIND_ENTITY)] = _sensor_selector()
        schema[vol.Required(CONF_WIND_MOUNTING, default=DEFAULT_WIND_MOUNTING)] = (
            _select(list(WIND_MOUNTINGS), CONF_WIND_MOUNTING)
        )
    if spec.supports_sun:
        has_weather = bool(_hass(handler).states.async_entity_ids("weather"))
        default_source = SUN_SOURCE_WEATHER if has_weather else SUN_SOURCE_NONE
        schema[vol.Required(CONF_SUN_SOURCE, default=default_source)] = _select(
            list(SUN_SOURCES), CONF_SUN_SOURCE, list_mode=True
        )
    return vol.Schema(schema)


def _check_unit(
    hass: HomeAssistant,
    entity_id: str,
    valid: tuple[str | None, ...] | set[str | None],
    error: str,
) -> None:
    """Raise if an available entity reports a unit we cannot use."""
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return
    if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) not in valid:
        raise SchemaFlowError(error)


async def _validate_sensors(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    hass = _hass(handler)
    spec = METHOD_SPECS[handler.options[CONF_METHOD]]

    _check_unit(
        hass,
        user_input[CONF_TEMPERATURE_ENTITY],
        {None, *TemperatureConverter.VALID_UNITS},
        "temperature_unit",
    )
    if spec.needs_humidity:
        _check_unit(
            hass, user_input[CONF_HUMIDITY_ENTITY], (None, PERCENTAGE), "humidity_unit"
        )
    if spec.needs_wind:
        _check_unit(
            hass,
            user_input[CONF_WIND_ENTITY],
            {None, *SpeedConverter.VALID_UNITS},
            "wind_unit",
        )

    # Drop settings that no longer apply (e.g. after switching method).
    options = handler.options
    if not spec.needs_humidity:
        options.pop(CONF_HUMIDITY_ENTITY, None)
    if not spec.needs_wind:
        for key in (CONF_WIND_ENTITY, CONF_WIND_MOUNTING, CONF_WIND_FACTOR):
            options.pop(key, None)
    elif user_input.get(CONF_WIND_MOUNTING) != WIND_MOUNTING_CUSTOM:
        options.pop(CONF_WIND_FACTOR, None)
    if not spec.supports_sun or user_input.get(CONF_SUN_SOURCE) == SUN_SOURCE_NONE:
        for key in _SUN_KEYS:
            options.pop(key, None)
    return user_input


def _needs_custom_wind(options: dict[str, Any]) -> bool:
    spec = METHOD_SPECS[options[CONF_METHOD]]
    return spec.needs_wind and options.get(CONF_WIND_MOUNTING) == WIND_MOUNTING_CUSTOM


async def _next_after_sensors(options: dict[str, Any]) -> str | None:
    spec = METHOD_SPECS[options[CONF_METHOD]]
    if (
        spec.supports_sun
        and options.get(CONF_SUN_SOURCE, SUN_SOURCE_NONE) != SUN_SOURCE_NONE
    ):
        return "sun"
    return "custom_wind" if _needs_custom_wind(options) else None


# ------------------------------------------------------------------------ sun


async def _sun_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    source = handler.options[CONF_SUN_SOURCE]
    schema: dict[Any, Any] = {}
    if source == SUN_SOURCE_IRRADIANCE:
        schema[vol.Required(CONF_IRRADIANCE_ENTITY)] = _sensor_selector()
    elif source == SUN_SOURCE_ILLUMINANCE:
        schema[vol.Required(CONF_ILLUMINANCE_ENTITY)] = _sensor_selector()
        schema[vol.Required(CONF_LUX_PER_WM2, default=DEFAULT_LUX_PER_WM2)] = (
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50, max=200, step=1, mode=selector.NumberSelectorMode.BOX
                )
            )
        )
    elif source == SUN_SOURCE_WEATHER:
        schema[vol.Required(CONF_WEATHER_ENTITY)] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="weather")
        )
    schema[vol.Required(CONF_GROUND_SURFACE, default=DEFAULT_GROUND_SURFACE)] = _select(
        list(GROUND_ALBEDO), CONF_GROUND_SURFACE
    )
    schema[vol.Optional(CONF_SNOW_ENTITY)] = selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["binary_sensor", "input_boolean", "sensor"]
        )
    )
    return vol.Schema(schema)


async def _validate_sun(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    hass = _hass(handler)
    source = handler.options[CONF_SUN_SOURCE]
    if source == SUN_SOURCE_IRRADIANCE:
        _check_unit(
            hass,
            user_input[CONF_IRRADIANCE_ENTITY],
            (None, *_IRRADIANCE_UNITS),
            "irradiance_unit",
        )
    elif source == SUN_SOURCE_ILLUMINANCE:
        _check_unit(
            hass,
            user_input[CONF_ILLUMINANCE_ENTITY],
            (None, LIGHT_LUX),
            "illuminance_unit",
        )
    elif source == SUN_SOURCE_WEATHER:
        state = hass.states.get(user_input[CONF_WEATHER_ENTITY])
        if (
            state is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and "cloud_coverage" not in state.attributes
        ):
            raise SchemaFlowError("weather_cloud_coverage")

    # Keep only the entity for the selected source.
    for other, key in SUN_SOURCE_ENTITY_KEYS.items():
        if other != source:
            handler.options.pop(key, None)
    if source != SUN_SOURCE_ILLUMINANCE:
        handler.options.pop(CONF_LUX_PER_WM2, None)
    return user_input


async def _next_after_sun(options: dict[str, Any]) -> str | None:
    return "custom_wind" if _needs_custom_wind(options) else None


# ----------------------------------------------------------------- custom wind

CUSTOM_WIND_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_WIND_FACTOR, default=WIND_MOUNTING_FACTORS[DEFAULT_WIND_MOUNTING]
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.5, max=3.0, step=0.01, mode=selector.NumberSelectorMode.BOX
            )
        )
    }
)

# ----------------------------------------------------------------------- flows

_COMMON_STEPS = {
    "sensors": SchemaFlowFormStep(
        _sensors_schema,
        validate_user_input=_validate_sensors,
        next_step=_next_after_sensors,
    ),
    "sun": SchemaFlowFormStep(
        _sun_schema, validate_user_input=_validate_sun, next_step=_next_after_sun
    ),
    "custom_wind": SchemaFlowFormStep(CUSTOM_WIND_SCHEMA),
}

CONFIG_FLOW = {
    "user": SchemaFlowFormStep(_method_schema, next_step="sensors"),
    **_COMMON_STEPS,
}

OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(_method_schema, next_step="sensors"),
    **_COMMON_STEPS,
}


class FeelsLikeConfigFlow(SchemaConfigFlowHandler, domain=DOMAIN):
    """Create and edit Feels Like."""

    config_flow = CONFIG_FLOW
    options_flow = OPTIONS_FLOW
    options_flow_reloads = True

    VERSION = 1
    MINOR_VERSION = 1

    @callback
    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Use the user-given name as title."""
        return cast(str, options[CONF_NAME])
