"""Tolerate common non-standard spellings of input units.

Template, MQTT and other user-defined sensors often report units that mean the
same thing as Home Assistant's canonical unit but are spelled differently (an
ordinal sign instead of a degree sign, "W/m2", "lux"). They are mapped to the
canonical unit before any unit check or conversion.
"""

from __future__ import annotations

from typing import Final

from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    UnitOfIrradiance,
    UnitOfSpeed,
    UnitOfTemperature,
)

_ALIASES: Final[dict[str, str]] = {
    # Temperature: "º" is the ordinal indicator, "˚" the ring above.
    "C": UnitOfTemperature.CELSIUS,
    "ºC": UnitOfTemperature.CELSIUS,
    "˚C": UnitOfTemperature.CELSIUS,
    "degC": UnitOfTemperature.CELSIUS,
    "F": UnitOfTemperature.FAHRENHEIT,
    "ºF": UnitOfTemperature.FAHRENHEIT,
    "˚F": UnitOfTemperature.FAHRENHEIT,
    "degF": UnitOfTemperature.FAHRENHEIT,
    # Relative humidity
    "%RH": PERCENTAGE,
    "% RH": PERCENTAGE,
    "%rh": PERCENTAGE,
    # Speed
    "kph": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "kmh": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "km/hr": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "mps": UnitOfSpeed.METERS_PER_SECOND,
    "kt": UnitOfSpeed.KNOTS,
    "kts": UnitOfSpeed.KNOTS,
    "knots": UnitOfSpeed.KNOTS,
    # Irradiance
    "W/m2": UnitOfIrradiance.WATTS_PER_SQUARE_METER,
    "W/m^2": UnitOfIrradiance.WATTS_PER_SQUARE_METER,
    "W m-2": UnitOfIrradiance.WATTS_PER_SQUARE_METER,
    # Illuminance
    "lux": LIGHT_LUX,
    "Lux": LIGHT_LUX,
}


def normalize_unit(unit: object) -> str | None:
    """Return the canonical spelling of a unit attribute (None if absent)."""
    if unit is None:
        return None
    text = str(unit).strip()
    if not text:
        return None
    return _ALIASES.get(text, text)
