"""Feels Like: real-feel temperature helpers for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import FeelsLikeCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

type FeelsLikeConfigEntry = ConfigEntry[FeelsLikeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: FeelsLikeConfigEntry) -> bool:
    """Set up a helper from a config entry."""
    coordinator = FeelsLikeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_track_inputs())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FeelsLikeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
