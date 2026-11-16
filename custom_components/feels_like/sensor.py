"""Sensor entities for the Feels Like integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FeelsLikeConfigEntry
from .const import (
    ATTR_ALBEDO,
    ATTR_BASED_ON,
    ATTR_BLEND_WEIGHT,
    ATTR_CLAMPED,
    ATTR_DIFFUSE_IRRADIANCE,
    ATTR_DIRECT_IRRADIANCE,
    ATTR_IRRADIANCE,
    ATTR_IRRADIANCE_SOURCE,
    ATTR_MEAN_RADIANT_TEMPERATURE,
    ATTR_METHOD,
    ATTR_METHOD_USED,
    ATTR_SEASON,
    ATTR_SOLAR_ELEVATION,
    ATTR_UTCI,
    ATTR_VAPOUR_PRESSURE,
    ATTR_WIND_SPEED_10M,
    DOMAIN,
    METHOD_SPECS,
    SUN_SOURCE_NONE,
)
from .coordinator import FeelsLikeCoordinator, FeelsLikeData
from .formulas.utci import UTCI_CATEGORIES

KIND_PRIMARY = "primary"
KIND_SUN = "sun"

# Diagnostic attributes change constantly; keep them out of the recorder.
_UNRECORDED = frozenset(
    {
        ATTR_SOLAR_ELEVATION,
        ATTR_IRRADIANCE,
        ATTR_DIRECT_IRRADIANCE,
        ATTR_DIFFUSE_IRRADIANCE,
        ATTR_MEAN_RADIANT_TEMPERATURE,
        ATTR_WIND_SPEED_10M,
        ATTR_VAPOUR_PRESSURE,
        ATTR_BLEND_WEIGHT,
        ATTR_ALBEDO,
        ATTR_UTCI,
    }
)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FeelsLikeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors for one helper."""
    coordinator = entry.runtime_data
    settings = coordinator.settings
    spec = METHOD_SPECS[settings.method]
    has_sun = spec.supports_sun and settings.sun_source != SUN_SOURCE_NONE

    entities: list[SensorEntity] = [
        FeelsLikeSensor(coordinator, entry, KIND_PRIMARY, has_sun=has_sun)
    ]
    if has_sun:
        entities.append(FeelsLikeSensor(coordinator, entry, KIND_SUN, has_sun=True))
    if spec.has_stress_category:
        entities.append(ThermalStressSensor(coordinator, entry))
    async_add_entities(entities)


class FeelsLikeEntity(CoordinatorEntity[FeelsLikeCoordinator]):
    """Common base: one service device per helper."""

    _attr_has_entity_name = True
    _unrecorded_attributes = _UNRECORDED

    def __init__(
        self, coordinator: FeelsLikeCoordinator, entry: FeelsLikeConfigEntry
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _data(self) -> FeelsLikeData | None:
        return self.coordinator.data


class FeelsLikeSensor(FeelsLikeEntity, SensorEntity):
    """Feels-like temperature (shade/primary or sun)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: FeelsLikeCoordinator,
        entry: FeelsLikeConfigEntry,
        kind: str,
        *,
        has_sun: bool,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, entry)
        self._kind = kind
        if kind == KIND_SUN:
            self._attr_unique_id = f"{entry.entry_id}_sun"
            self._attr_translation_key = "feels_like_sun"
        else:
            # The primary value keeps the same unique_id whether or not a sun
            # sensor exists, so switching methods keeps the entity_id.
            self._attr_unique_id = entry.entry_id
            if has_sun:
                self._attr_translation_key = "feels_like_shade"
            else:
                self._attr_translation_key = "feels_like"
                self._attr_name = None  # use the helper (device) name

    def _value(self) -> float | None:
        data = self._data
        if data is None or data.result is None:
            return None
        if self._kind == KIND_SUN:
            return data.result.sun
        return data.result.shade

    @property
    def available(self) -> bool:
        """Unavailable while inputs are missing."""
        return super().available and self._value() is not None

    @property
    def native_value(self) -> float | None:
        """Return the feels-like temperature."""
        return _round(self._value())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Explain how the value was obtained."""
        data = self._data
        if data is None or data.result is None:
            return None
        result = data.result
        attrs: dict[str, Any] = {
            ATTR_METHOD: self.coordinator.settings.method,
            ATTR_SEASON: data.season,
        }
        if self._kind == KIND_SUN:
            attrs[ATTR_MEAN_RADIANT_TEMPERATURE] = _round(result.mrt_sun)
        else:
            attrs[ATTR_METHOD_USED] = result.method_used
            attrs[ATTR_MEAN_RADIANT_TEMPERATURE] = _round(result.mrt_shade)
            attrs[ATTR_BLEND_WEIGHT] = _round(result.blend_weight)
        attrs[ATTR_WIND_SPEED_10M] = _round(data.wind_10m)
        attrs[ATTR_VAPOUR_PRESSURE] = _round(result.vapour_pressure)
        if data.irradiance_source is not None:
            attrs[ATTR_SOLAR_ELEVATION] = _round(data.solar_elevation, 1)
            attrs[ATTR_IRRADIANCE] = _round(data.irradiance, 1)
            attrs[ATTR_IRRADIANCE_SOURCE] = data.irradiance_source
            attrs[ATTR_DIRECT_IRRADIANCE] = _round(result.dni, 1)
            attrs[ATTR_DIFFUSE_IRRADIANCE] = _round(result.dhi, 1)
            attrs[ATTR_ALBEDO] = data.albedo
        attrs[ATTR_CLAMPED] = result.clamped
        return {key: value for key, value in attrs.items() if value is not None}


class ThermalStressSensor(FeelsLikeEntity, SensorEntity):
    """UTCI thermal stress category."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(UTCI_CATEGORIES)
    _attr_translation_key = "thermal_stress"

    def __init__(
        self, coordinator: FeelsLikeCoordinator, entry: FeelsLikeConfigEntry
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_thermal_stress"

    @property
    def available(self) -> bool:
        """Unavailable while inputs are missing."""
        data = self._data
        return (
            super().available
            and data is not None
            and data.result is not None
            and data.result.stress is not None
        )

    @property
    def native_value(self) -> str | None:
        """Return the stress category."""
        data = self._data
        if data is None or data.result is None:
            return None
        return data.result.stress

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Report the UTCI value the category is based on."""
        data = self._data
        if data is None or data.result is None:
            return None
        result = data.result
        based_on_sun = result.sun is not None
        return {
            ATTR_UTCI: _round(result.sun if based_on_sun else result.shade),
            ATTR_BASED_ON: "sun" if based_on_sun else "shade",
        }
