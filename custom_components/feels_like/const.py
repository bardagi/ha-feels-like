"""Constants for the Feels Like integration."""

from __future__ import annotations

from typing import Final

from .formulas.feels_like import (  # noqa: F401 (re-exported)
    METHOD_BOM,
    METHOD_FOUR_SEASON,
    METHOD_HEAT_INDEX,
    METHOD_HUMIDEX,
    METHOD_SPECS,
    METHOD_UTCI,
    METHOD_WIND_CHILL,
    METHODS,
)

DOMAIN: Final = "feels_like"

CONF_METHOD: Final = "method"
CONF_TEMPERATURE_ENTITY: Final = "temperature_entity"
CONF_HUMIDITY_ENTITY: Final = "humidity_entity"
CONF_WIND_ENTITY: Final = "wind_entity"
CONF_WIND_MOUNTING: Final = "wind_mounting"
CONF_WIND_FACTOR: Final = "wind_factor"
CONF_SUN_SOURCE: Final = "sun_source"
CONF_IRRADIANCE_ENTITY: Final = "irradiance_entity"
CONF_ILLUMINANCE_ENTITY: Final = "illuminance_entity"
CONF_LUX_PER_WM2: Final = "lux_per_wm2"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_GROUND_SURFACE: Final = "ground_surface"
CONF_SNOW_ENTITY: Final = "snow_entity"

DEFAULT_NAME: Final = "Feels like"
DEFAULT_METHOD: Final = METHOD_UTCI

# Sun sources
SUN_SOURCE_IRRADIANCE: Final = "irradiance"
SUN_SOURCE_ILLUMINANCE: Final = "illuminance"
SUN_SOURCE_WEATHER: Final = "weather"
SUN_SOURCE_NONE: Final = "none"
SUN_SOURCES: Final = (
    SUN_SOURCE_IRRADIANCE,
    SUN_SOURCE_ILLUMINANCE,
    SUN_SOURCE_WEATHER,
    SUN_SOURCE_NONE,
)
SUN_SOURCE_ENTITY_KEYS: Final = {
    SUN_SOURCE_IRRADIANCE: CONF_IRRADIANCE_ENTITY,
    SUN_SOURCE_ILLUMINANCE: CONF_ILLUMINANCE_ENTITY,
    SUN_SOURCE_WEATHER: CONF_WEATHER_ENTITY,
}
DEFAULT_LUX_PER_WM2: Final = 120.0

# Wind sensor mounting -> factor to the 10 m reference height. Log wind profile
# over open terrain (z0 = 0.03 m); face level uses the official JAG/TI 1.5.
WIND_MOUNTING_CUSTOM: Final = "custom"
WIND_MOUNTING_FACTORS: Final[dict[str, float]] = {
    "mast_10m": 1.00,
    "rooftop_5m": 1.14,
    "pole_3m": 1.26,
    "garden_2m": 1.38,
    "face_level": 1.50,
}
WIND_MOUNTINGS: Final = (*WIND_MOUNTING_FACTORS, WIND_MOUNTING_CUSTOM)
DEFAULT_WIND_MOUNTING: Final = "face_level"

# Ground surface -> short-wave albedo (typical textbook values).
GROUND_ALBEDO: Final[dict[str, float]] = {
    "grass": 0.20,
    "urban": 0.10,
    "light": 0.30,
    "snow": 0.70,
}
SNOW_ALBEDO: Final = GROUND_ALBEDO["snow"]
DEFAULT_GROUND_SURFACE: Final = "grass"

# Attributes
ATTR_METHOD: Final = "method"
ATTR_METHOD_USED: Final = "method_used"
ATTR_SEASON: Final = "season"
ATTR_SOLAR_ELEVATION: Final = "solar_elevation"
ATTR_IRRADIANCE: Final = "irradiance"
ATTR_IRRADIANCE_SOURCE: Final = "irradiance_source"
ATTR_DIRECT_IRRADIANCE: Final = "direct_irradiance"
ATTR_DIFFUSE_IRRADIANCE: Final = "diffuse_irradiance"
ATTR_MEAN_RADIANT_TEMPERATURE: Final = "mean_radiant_temperature"
ATTR_WIND_SPEED_10M: Final = "wind_speed_10m"
ATTR_VAPOUR_PRESSURE: Final = "vapour_pressure"
ATTR_BLEND_WEIGHT: Final = "blend_weight"
ATTR_CLAMPED: Final = "clamped"
ATTR_ALBEDO: Final = "albedo"
ATTR_UTCI: Final = "utci"
ATTR_BASED_ON: Final = "based_on"

UPDATE_INTERVAL_MINUTES: Final = 5

# Input changes within this window (seconds) are merged into one update.
CONF_MIN_UPDATE_INTERVAL: Final = "min_update_interval"
MIN_UPDATE_INTERVALS: Final = ("10", "30", "60", "300")
DEFAULT_MIN_UPDATE_INTERVAL: Final = "60"
