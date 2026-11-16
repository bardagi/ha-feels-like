"""Coordinator: reads input entities and computes feels-like values."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any

from astral import Observer
from astral.sun import elevation as astral_elevation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    LIGHT_LUX,
    PERCENTAGE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfIrradiance,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    CONF_BACKUP_WEATHER_ENTITY,
    CONF_GROUND_SURFACE,
    CONF_HOLD_TIME,
    CONF_HUMIDITY_ENTITY,
    CONF_LUX_PER_WM2,
    CONF_METHOD,
    CONF_MIN_UPDATE_INTERVAL,
    CONF_PRECISION,
    CONF_SNOW_ENTITY,
    CONF_SUN_SOURCE,
    CONF_TEMPERATURE_ENTITY,
    CONF_WIND_ENTITY,
    CONF_WIND_FACTOR,
    CONF_WIND_MOUNTING,
    DEFAULT_GROUND_SURFACE,
    DEFAULT_HOLD_TIME,
    DEFAULT_LUX_PER_WM2,
    DEFAULT_MIN_UPDATE_INTERVAL,
    DEFAULT_PRECISION,
    DEFAULT_WIND_MOUNTING,
    DOMAIN,
    GROUND_ALBEDO,
    METHOD_SPECS,
    PRECISIONS,
    SNOW_ALBEDO,
    SUN_SOURCE_ENTITY_KEYS,
    SUN_SOURCE_ILLUMINANCE,
    SUN_SOURCE_NONE,
    SUN_SOURCE_WEATHER,
    SUN_SOURCES,
    UPDATE_INTERVAL_MINUTES,
    WIND_MOUNTING_CUSTOM,
    WIND_MOUNTING_FACTORS,
    WIND_MOUNTINGS,
)
from .formulas import solar
from .formulas.feels_like import FeelsLikeInputs, FeelsLikeResult, compute
from .formulas.season import meteorological_season
from .units import normalize_unit

_LOGGER = logging.getLogger(__name__)

BTU_PER_HOUR_SQFT_TO_W_PER_M2 = 3.15459
_UNAVAILABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)

# Weather entity attributes.
ATTR_CLOUD_COVERAGE = "cloud_coverage"
ATTR_WEATHER_TEMPERATURE = "temperature"
ATTR_WEATHER_TEMPERATURE_UNIT = "temperature_unit"
ATTR_WEATHER_HUMIDITY = "humidity"
ATTR_WEATHER_WIND_SPEED = "wind_speed"
ATTR_WEATHER_WIND_SPEED_UNIT = "wind_speed_unit"

# Readings outside these ranges are sensor glitches, handled like a dropout.
TEMPERATURE_RANGE = (-90.0, 70.0)  # °C
# Sensors in fog, or with a calibration offset, read a little above 100 %:
# such values count as 100 %; only larger ones are glitches.
HUMIDITY_RANGE = (0.0, 110.0)  # %
MAX_HUMIDITY = 100.0  # %
MAX_WIND_SPEED = 75.0  # m/s, as measured (before the mounting factor)
MAX_IRRADIANCE = 2000.0  # W/m², above the highest values measured on the ground

# A measured sun input is held relative to clear sky, so a held value follows
# the sun. The clear-sky model is only used where it is meaningful (sun above
# about 5°), and the ratio is capped (cloud edges can briefly exceed clear sky).
MIN_CLEAR_SKY_FOR_INDEX = 50.0  # W/m²
MAX_CLEAR_SKY_INDEX = 1.5

# An input out this long is reported even if it never had a value to hold; a
# missing entity is then most likely renamed or deleted.
OUT_TOO_LONG = timedelta(minutes=10)

# Last good input values are saved so the hold time bridges restarts.
STORAGE_VERSION = 1
SAVE_DELAY = 60  # seconds; pending values are always written when HA stops

# Values of the irradiance_source attribute.
IRRADIANCE_SOURCE_SENSOR = "sensor"
IRRADIANCE_SOURCE_ILLUMINANCE = "illuminance"
IRRADIANCE_SOURCE_ESTIMATED = "estimated"
IRRADIANCE_SOURCE_NIGHT = "night"

# Keys of the last-good-value cache.
_TEMPERATURE = "temperature"
_HUMIDITY = "humidity"
_WIND = "wind"
_SUN = "sun"
_SNOW = "snow"


class InputError(Exception):
    """An input entity is missing, unavailable or has an unsupported value."""

    def __init__(
        self, message: str, *, config_problem: bool = False, missing: bool = False
    ) -> None:
        """Initialise.

        config_problem marks errors the user must fix; missing marks an entity
        that does not exist (yet).
        """
        super().__init__(message)
        self.config_problem = config_problem
        self.missing = missing


class _Origin(Enum):
    """Where an input value came from."""

    LIVE = auto()
    HELD = auto()
    BACKUP = auto()


@dataclass(slots=True)
class _Status:
    """What happened to the inputs during one calculation."""

    held: list[str] = field(default_factory=list)
    backup: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    problems: list[tuple[str, int]] = field(default_factory=list)

    def report(self, problem: str, level: int) -> None:
        """Record a problem to log at the given level."""
        self.problems.append((problem, level))


# ------------------------------------------------------------------ settings


def _invalid(key: str, value: Any) -> ConfigEntryError:
    return ConfigEntryError(f"Invalid {key} '{value}'; reconfigure the helper")


def _choice(
    options: Mapping[str, Any], key: str, default: str | None, valid: Collection[str]
) -> str:
    value = options.get(key, default)
    if not isinstance(value, str) or value not in valid:
        raise _invalid(key, value)
    return value


def _number(
    options: Mapping[str, Any], key: str, default: Any, *, positive: bool = False
) -> float:
    raw = options.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _invalid(key, raw) from None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise _invalid(key, raw)
    return value


def _optional_entity(options: Mapping[str, Any], key: str) -> str | None:
    value = options.get(key) or None
    if value is not None and not isinstance(value, str):
        raise _invalid(key, value)
    return value


def _entity(options: Mapping[str, Any], key: str) -> str:
    value = _optional_entity(options, key)
    if value is None:
        raise ConfigEntryError(f"No {key} is configured; reconfigure the helper")
    return value


def _precision(options: Mapping[str, Any]) -> int:
    value = str(options.get(CONF_PRECISION, DEFAULT_PRECISION))
    if value not in PRECISIONS:
        raise _invalid(CONF_PRECISION, value)
    return int(value)


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration of one helper."""

    method: str
    temperature_entity: str
    humidity_entity: str | None
    wind_entity: str | None
    wind_factor: float
    sun_source: str
    sun_entity: str | None
    lux_per_wm2: float
    ground_albedo: float
    snow_entity: str | None
    min_update_interval: float
    hold_time: timedelta = timedelta(seconds=float(DEFAULT_HOLD_TIME))
    backup_weather_entity: str | None = None
    precision: int = int(DEFAULT_PRECISION)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        """Build settings from config entry options.

        Raises ConfigEntryError for options that cannot be used (an entry
        edited by hand or written by another version), so the problem is
        reported once at setup rather than as a failure on every update.
        """
        method = _choice(options, CONF_METHOD, None, METHOD_SPECS)
        spec = METHOD_SPECS[method]

        wind_factor = 1.0
        if spec.needs_wind:
            mounting = _choice(
                options, CONF_WIND_MOUNTING, DEFAULT_WIND_MOUNTING, WIND_MOUNTINGS
            )
            if mounting == WIND_MOUNTING_CUSTOM:
                wind_factor = _number(options, CONF_WIND_FACTOR, 1.0, positive=True)
            else:
                wind_factor = WIND_MOUNTING_FACTORS[mounting]

        sun_source = (
            _choice(options, CONF_SUN_SOURCE, SUN_SOURCE_NONE, SUN_SOURCES)
            if spec.supports_sun
            else SUN_SOURCE_NONE
        )
        has_sun = sun_source != SUN_SOURCE_NONE

        return cls(
            method=method,
            temperature_entity=_entity(options, CONF_TEMPERATURE_ENTITY),
            humidity_entity=(
                _entity(options, CONF_HUMIDITY_ENTITY) if spec.needs_humidity else None
            ),
            wind_entity=_entity(options, CONF_WIND_ENTITY) if spec.needs_wind else None,
            wind_factor=wind_factor,
            sun_source=sun_source,
            sun_entity=(
                _entity(options, SUN_SOURCE_ENTITY_KEYS[sun_source])
                if has_sun
                else None
            ),
            lux_per_wm2=(
                _number(options, CONF_LUX_PER_WM2, DEFAULT_LUX_PER_WM2, positive=True)
                if sun_source == SUN_SOURCE_ILLUMINANCE
                else DEFAULT_LUX_PER_WM2
            ),
            ground_albedo=GROUND_ALBEDO[
                _choice(
                    options, CONF_GROUND_SURFACE, DEFAULT_GROUND_SURFACE, GROUND_ALBEDO
                )
            ],
            snow_entity=_optional_entity(options, CONF_SNOW_ENTITY)
            if has_sun
            else None,
            min_update_interval=_number(
                options, CONF_MIN_UPDATE_INTERVAL, DEFAULT_MIN_UPDATE_INTERVAL
            ),
            hold_time=timedelta(
                seconds=_number(options, CONF_HOLD_TIME, DEFAULT_HOLD_TIME)
            ),
            backup_weather_entity=_optional_entity(options, CONF_BACKUP_WEATHER_ENTITY),
            precision=_precision(options),
        )

    @property
    def has_sun(self) -> bool:
        """Whether a sun input is configured (and the method uses it)."""
        return self.sun_entity is not None

    @property
    def input_entities(self) -> dict[str, str | None]:
        """Input entity behind each key of the last-good-value cache."""
        return {
            _TEMPERATURE: self.temperature_entity,
            _HUMIDITY: self.humidity_entity,
            _WIND: self.wind_entity,
            _SUN: self.sun_entity,
            _SNOW: self.snow_entity,
        }

    @property
    def tracked_entities(self) -> list[str]:
        """Entities whose state changes trigger a recalculation.

        The backup weather entity is not tracked: it is only read while an input
        is out, and the periodic refresh picks up its changes.
        """
        return [entity_id for entity_id in self.input_entities.values() if entity_id]


@dataclass(frozen=True, slots=True)
class FeelsLikeData:
    """Snapshot shared by all entities of one helper."""

    result: FeelsLikeResult | None
    error: str | None
    season: str
    solar_elevation: float
    wind_10m: float | None = None
    irradiance: float | None = None
    irradiance_source: str | None = None
    albedo: float | None = None
    held_inputs: tuple[str, ...] = ()
    """Input entities that are out; their last good value is used."""
    backup_inputs: tuple[str, ...] = ()
    """Input entities that are out; the backup weather entity is used instead."""


# ------------------------------------------------------------- value helpers


def _assess(err: InputError, out_for: timedelta) -> tuple[str, int]:
    """Return a hint to add to an input problem and the level to log it at."""
    if err.missing and out_for >= OUT_TOO_LONG:
        # Gone for this long: most likely renamed or deleted.
        return "; check the helper's configuration", logging.WARNING
    return "", logging.WARNING if err.config_problem else logging.DEBUG


def _attribute_value(
    state: State, entity_id: str, attribute: str, *, config_problem: bool = False
) -> float:
    raw = state.attributes.get(attribute)
    if raw is None:
        raise InputError(
            f"{entity_id} has no {attribute} attribute", config_problem=config_problem
        )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value):
        raise InputError(
            f"{entity_id} has non-numeric {attribute} '{raw}'",
            config_problem=config_problem,
        )
    return value


def _to_celsius(entity_id: str, value: float, unit: str) -> float:
    if unit not in TemperatureConverter.VALID_UNITS:
        raise InputError(
            f"{entity_id} has unsupported unit '{unit}'", config_problem=True
        )
    celsius = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
    low, high = TEMPERATURE_RANGE
    if not low <= celsius <= high:
        raise InputError(f"{entity_id} has implausible temperature {celsius:g} °C")
    return celsius


def _check_humidity(entity_id: str, value: float) -> float:
    low, high = HUMIDITY_RANGE
    if not low <= value <= high:
        raise InputError(f"{entity_id} has implausible humidity {value:g} %")
    return min(value, MAX_HUMIDITY)


def _to_meters_per_second(entity_id: str, value: float, unit: str) -> float:
    if unit not in SpeedConverter.VALID_UNITS:
        raise InputError(
            f"{entity_id} has unsupported unit '{unit}'", config_problem=True
        )
    speed = SpeedConverter.convert(value, unit, UnitOfSpeed.METERS_PER_SECOND)
    if speed > MAX_WIND_SPEED:
        raise InputError(f"{entity_id} has implausible wind speed {speed:g} m/s")
    return max(speed, 0.0)


def _check_irradiance(entity_id: str, value: float) -> float:
    if value > MAX_IRRADIANCE:
        raise InputError(f"{entity_id} has implausible irradiance {value:g} W/m²")
    return max(value, 0.0)


def _restored_value(
    record: Any, entity_id: str, now: datetime, hold_time: timedelta
) -> tuple[float, datetime] | None:
    """A saved last good value, if valid, of this entity and still within hold."""
    if not isinstance(record, dict) or record.get("entity_id") != entity_id:
        return None
    value = record.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    read_at = dt_util.parse_datetime(str(record.get("time")))
    if not math.isfinite(value) or read_at is None or read_at.tzinfo is None:
        return None
    if not timedelta(0) <= now - read_at < hold_time:
        return None
    return float(value), read_at


class FeelsLikeCoordinator(DataUpdateCoordinator[FeelsLikeData]):
    """Compute feels-like values for one helper config entry."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        self.settings = Settings.from_options(entry.options)
        # Input changes run a refresh at once, then at most once per cooldown.
        self._input_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=self.settings.min_update_interval,
            immediate=True,
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
            always_update=False,
            request_refresh_debouncer=self._input_debouncer,
        )
        self._last_problem: str | None = None
        self._last_problem_level = logging.NOTSET
        # Last good value of each input and when it was read.
        self._last_good: dict[str, tuple[float, datetime]] = {}
        # When each input that is out now was first found out.
        self._out_since: dict[str, datetime] = {}
        self._store = holds_store(hass, entry.entry_id)

    async def _async_update_data(self) -> FeelsLikeData:
        """Recalculate (on schedule and on throttled input changes)."""
        try:
            return self.calculate()
        except (ArithmeticError, ValueError) as err:
            # Logged once by the coordinator, not on every refresh.
            raise UpdateFailed(f"Calculation failed: {err}") from err

    @callback
    def async_track_inputs(self) -> CALLBACK_TYPE:
        """Recalculate when an input entity changes, throttled by the debouncer."""

        @callback
        def _state_changed(event: Event[EventStateChangedData]) -> None:  # noqa: ARG001
            self._input_debouncer.async_schedule_call()

        return async_track_state_change_event(
            self.hass, self.settings.tracked_entities, _state_changed
        )

    # ------------------------------------------------------------ persistence

    async def async_restore(self) -> None:
        """Restore last good values saved before a restart or reload."""
        try:
            stored = await self._store.async_load()
        except HomeAssistantError as err:
            _LOGGER.debug(
                "%s: cannot load saved input values: %s", self.config_entry.title, err
            )
            return
        inputs = stored.get("inputs") if isinstance(stored, dict) else None
        if not isinstance(inputs, dict):
            return
        now = dt_util.utcnow()
        for key, entity_id in self.settings.input_entities.items():
            if entity_id is None:
                continue
            restored = _restored_value(
                inputs.get(key), entity_id, now, self.settings.hold_time
            )
            if restored is not None:
                self._last_good[key] = restored

    async def async_save_holds(self) -> None:
        """Save the last good values now (a reload reads them back at once)."""
        await self._store.async_save(self._holds_to_save())

    def _holds_to_save(self) -> dict[str, Any]:
        entities = self.settings.input_entities
        return {
            "inputs": {
                key: {
                    "entity_id": entities[key],
                    "value": value,
                    "time": read_at.isoformat(),
                }
                for key, (value, read_at) in self._last_good.items()
                if entities.get(key)
            }
        }

    # ------------------------------------------------------------ resolution

    def _out_for(self, key: str, now: datetime) -> timedelta:
        """How long an input has been out (zero on its first failure)."""
        return now - self._out_since.setdefault(key, now)

    def _read_live(
        self,
        key: str,
        entity_id: str,
        read: Callable[[str], float],
        now: datetime,
    ) -> float:
        """Read an input entity and remember the value as its last good one."""
        value = read(entity_id)
        self._last_good[key] = (value, now)
        self._out_since.pop(key, None)
        return value

    def _substitute(
        self,
        key: str,
        entity_id: str,
        err: InputError,
        backup: Callable[[str], float] | None,
        now: datetime,
        status: _Status,
    ) -> tuple[float, _Origin]:
        """Replace a failed input by its held value or the backup weather entity.

        Re-raises the input's error if neither is available.
        """
        hold_time = self.settings.hold_time
        out_for = self._out_for(key, now)
        hint, level = _assess(err, out_for)
        last = self._last_good.get(key)
        if last is not None and now - last[1] < hold_time:
            status.held.append(entity_id)
            status.report(f"{err}{hint}; using its last value", level)
            return last[0], _Origin.HELD

        problem = f"{err}{hint}"
        if last is not None:
            # Out for longer than the hold time: worth telling the user.
            problem = f"{err} for longer than the hold time{hint}"
            level = max(level, logging.INFO)
        elif out_for >= (report_after := max(hold_time, OUT_TOO_LONG)):
            # Never had a value to hold (e.g. out since start-up).
            minutes = round(report_after.total_seconds() / 60)
            problem = f"{err} for more than {minutes} minutes{hint}"
            level = max(level, logging.INFO)
        weather = self.settings.backup_weather_entity
        if backup is not None and weather is not None:
            try:
                value = backup(weather)
            except InputError as backup_err:
                problem = f"{problem}, and the backup failed ({backup_err})"
            else:
                status.backup.append(entity_id)
                status.report(f"{problem}; using {weather}", max(level, logging.INFO))
                return value, _Origin.BACKUP
        status.report(problem, level)
        raise err

    def _resolve(
        self,
        key: str,
        entity_id: str,
        read: Callable[[str], float],
        backup: Callable[[str], float] | None,
        now: datetime,
        status: _Status,
    ) -> tuple[float, _Origin]:
        """Read an input: live, else held, else from the backup weather entity."""
        try:
            return self._read_live(key, entity_id, read, now), _Origin.LIVE
        except InputError as err:
            return self._substitute(key, entity_id, err, backup, now, status)

    def _required(
        self,
        key: str,
        entity_id: str | None,
        read: Callable[[str], float],
        backup: Callable[[str], float],
        now: datetime,
        status: _Status,
    ) -> float | None:
        """Resolve an input the result needs; None if unused or unavailable."""
        if entity_id is None:
            return None
        try:
            value, _origin = self._resolve(key, entity_id, read, backup, now, status)
        except InputError as err:
            status.failed.append(str(err))
            return None
        return value

    # ---------------------------------------------------------------- readers

    def _available_state(self, entity_id: str) -> State:
        state = self.hass.states.get(entity_id)
        if state is None:
            raise InputError(f"{entity_id} does not exist", missing=True)
        if state.state in _UNAVAILABLE_STATES:
            raise InputError(f"{entity_id} is {state.state}")
        return state

    def _numeric_state(self, entity_id: str) -> tuple[float, State]:
        state = self._available_state(entity_id)
        try:
            value = float(state.state)
        except ValueError as err:
            raise InputError(
                f"{entity_id} has non-numeric state '{state.state}'",
                config_problem=True,
            ) from err
        if not math.isfinite(value):
            raise InputError(f"{entity_id} is {state.state}")
        return value, state

    def _read_temperature(self, entity_id: str) -> float:
        value, state = self._numeric_state(entity_id)
        unit = (
            normalize_unit(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
            or UnitOfTemperature.CELSIUS
        )
        return _to_celsius(entity_id, value, unit)

    def _backup_temperature(self, entity_id: str) -> float:
        state = self._available_state(entity_id)
        value = _attribute_value(state, entity_id, ATTR_WEATHER_TEMPERATURE)
        unit = (
            normalize_unit(state.attributes.get(ATTR_WEATHER_TEMPERATURE_UNIT))
            or self.hass.config.units.temperature_unit
        )
        return _to_celsius(entity_id, value, unit)

    def _read_humidity(self, entity_id: str) -> float:
        value, state = self._numeric_state(entity_id)
        unit = normalize_unit(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        if unit not in (None, PERCENTAGE):
            raise InputError(
                f"{entity_id} has unsupported unit '{unit}'", config_problem=True
            )
        return _check_humidity(entity_id, value)

    def _backup_humidity(self, entity_id: str) -> float:
        state = self._available_state(entity_id)
        value = _attribute_value(state, entity_id, ATTR_WEATHER_HUMIDITY)
        return _check_humidity(entity_id, value)

    def _read_wind(self, entity_id: str) -> float:
        """Wind at 10 m from the wind sensor (mounting factor applied)."""
        value, state = self._numeric_state(entity_id)
        unit = (
            normalize_unit(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
            or UnitOfSpeed.METERS_PER_SECOND
        )
        speed = _to_meters_per_second(entity_id, value, unit)
        return speed * self.settings.wind_factor

    def _backup_wind(self, entity_id: str) -> float:
        """Wind at 10 m from a weather entity (forecasts are already at 10 m)."""
        state = self._available_state(entity_id)
        value = _attribute_value(state, entity_id, ATTR_WEATHER_WIND_SPEED)
        unit = (
            normalize_unit(state.attributes.get(ATTR_WEATHER_WIND_SPEED_UNIT))
            or self.hass.config.units.wind_speed_unit
        )
        return _to_meters_per_second(entity_id, value, unit)

    def _cloud_coverage(self, entity_id: str) -> float:
        state = self._available_state(entity_id)
        return _attribute_value(state, entity_id, ATTR_CLOUD_COVERAGE)

    def _read_sun(self, entity_id: str) -> float:
        """Raw sun input: W/m² for sensor sources, cloud cover % for weather."""
        source = self.settings.sun_source
        if source == SUN_SOURCE_WEATHER:
            state = self._available_state(entity_id)
            # A weather entity without cloud cover cannot be used as sun source.
            return _attribute_value(
                state, entity_id, ATTR_CLOUD_COVERAGE, config_problem=True
            )

        value, state = self._numeric_state(entity_id)
        unit = normalize_unit(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        if source == SUN_SOURCE_ILLUMINANCE:
            if unit not in (None, LIGHT_LUX):
                raise InputError(
                    f"{entity_id} has unsupported unit '{unit}'", config_problem=True
                )
            irradiance = solar.lux_to_irradiance(value, self.settings.lux_per_wm2)
            return _check_irradiance(entity_id, irradiance)

        if unit == UnitOfIrradiance.BTUS_PER_HOUR_SQUARE_FOOT:
            value *= BTU_PER_HOUR_SQFT_TO_W_PER_M2
        elif unit not in (None, UnitOfIrradiance.WATTS_PER_SQUARE_METER):
            raise InputError(
                f"{entity_id} has unsupported unit '{unit}'", config_problem=True
            )
        return _check_irradiance(entity_id, value)

    def _read_snow(self, entity_id: str) -> float:
        """1 if there is snow on the ground, else 0."""
        state = self._available_state(entity_id)
        if state.state == STATE_ON:
            return 1.0
        try:
            depth = float(state.state)
        except ValueError:
            return 0.0  # "off" or another non-snow state
        return 1.0 if depth > 0 else 0.0

    # ------------------------------------------------------------ optional inputs

    def _remember_sun(self, raw: float, clear_sky: float, now: datetime) -> None:
        """Keep a live sun reading to hold through a later dropout.

        Cloud cover (weather source) is kept as it is. Measured irradiance is
        kept relative to clear sky, so a held value follows the sun instead of
        carrying the midday sun into the evening. Near the horizon the previous
        value is kept, as the clear-sky model means little there.
        """
        self._out_since.pop(_SUN, None)
        if self.settings.sun_source == SUN_SOURCE_WEATHER:
            self._last_good[_SUN] = (raw, now)
        elif clear_sky >= MIN_CLEAR_SKY_FOR_INDEX:
            index = min(raw / clear_sky, MAX_CLEAR_SKY_INDEX)
            self._last_good[_SUN] = (index, now)

    def _irradiance(
        self, elevation: float, now: datetime, status: _Status
    ) -> tuple[float | None, str | None]:
        """Return (global horizontal irradiance, source) or (None, None)."""
        source = self.settings.sun_source
        entity_id = self.settings.sun_entity
        if entity_id is None:
            return None, None

        clear_sky = solar.haurwitz_clear_sky(elevation)
        try:
            raw = self._read_sun(entity_id)
        except InputError as err:
            if elevation <= 0:
                # No sunlight anyway: a missing sun input does not matter.
                hint, level = _assess(err, self._out_for(_SUN, now))
                status.report(f"{err}{hint} (not needed while the sun is down)", level)
                return 0.0, IRRADIANCE_SOURCE_NIGHT
            try:
                raw, origin = self._substitute(
                    _SUN, entity_id, err, self._cloud_coverage, now, status
                )
            except InputError:
                return None, None
        else:
            origin = _Origin.LIVE
            self._remember_sun(raw, clear_sky, now)

        if source == SUN_SOURCE_WEATHER or origin is _Origin.BACKUP:
            # Cloud cover: estimate irradiance for the sun's current elevation.
            return solar.kasten_czeplak(clear_sky, raw), IRRADIANCE_SOURCE_ESTIMATED
        if origin is _Origin.HELD:
            raw *= clear_sky  # held as a clear-sky index, see _remember_sun
        if source == SUN_SOURCE_ILLUMINANCE:
            return raw, IRRADIANCE_SOURCE_ILLUMINANCE
        return raw, IRRADIANCE_SOURCE_SENSOR

    def _albedo(self, now: datetime, status: _Status) -> float:
        entity_id = self.settings.snow_entity
        if not entity_id:
            return self.settings.ground_albedo
        try:
            snow, _origin = self._resolve(
                _SNOW, entity_id, self._read_snow, None, now, status
            )
        except InputError:
            return self.settings.ground_albedo
        return SNOW_ALBEDO if snow > 0 else self.settings.ground_albedo

    # ------------------------------------------------------------- calculation

    def _report(self, status: _Status) -> None:
        """Log input problems once per change instead of on every update."""
        if status.problems:
            problem: str | None = "; ".join(text for text, _ in status.problems)
            level = max(level for _, level in status.problems)
        else:
            problem, level = None, logging.NOTSET
        if problem == self._last_problem:
            return
        title = self.config_entry.title
        if problem is None:
            # Transient unavailability (e.g. at startup) recovers silently;
            # only announce recovery from problems we told the user about.
            recovered = (
                logging.INFO
                if self._last_problem_level >= logging.INFO
                else logging.DEBUG
            )
            _LOGGER.log(recovered, "%s: all inputs available again", title)
        else:
            _LOGGER.log(level, "%s: %s", title, problem)
        self._last_problem = problem
        self._last_problem_level = level

    def calculate(self, now: datetime | None = None) -> FeelsLikeData:
        """Read inputs and compute all values."""
        now = now or dt_util.utcnow()
        latitude = self.hass.config.latitude
        observer = Observer(latitude, self.hass.config.longitude, 0)
        elevation = round(
            float(astral_elevation(observer, now, with_refraction=False)), 2
        )
        local = dt_util.as_local(now)
        season = meteorological_season(local.month, latitude)

        settings = self.settings
        status = _Status()
        temperature = self._required(
            _TEMPERATURE,
            settings.temperature_entity,
            self._read_temperature,
            self._backup_temperature,
            now,
            status,
        )
        humidity = self._required(
            _HUMIDITY,
            settings.humidity_entity,
            self._read_humidity,
            self._backup_humidity,
            now,
            status,
        )
        wind = self._required(
            _WIND, settings.wind_entity, self._read_wind, self._backup_wind, now, status
        )
        ghi, ghi_source = self._irradiance(elevation, now, status)
        albedo = self._albedo(now, status)
        self._report(status)
        self._store.async_delay_save(self._holds_to_save, SAVE_DELAY)
        held = tuple(status.held)
        backup = tuple(status.backup)

        if temperature is None or status.failed:
            return FeelsLikeData(
                result=None,
                error="; ".join(status.failed),
                season=season,
                solar_elevation=elevation,
                held_inputs=held,
                backup_inputs=backup,
            )

        result = compute(
            FeelsLikeInputs(
                method=settings.method,
                temperature=temperature,
                humidity=humidity,
                wind_10m=wind,
                ghi=ghi,
                elevation=elevation,
                day_of_year=local.timetuple().tm_yday,
                albedo=albedo,
            )
        )
        values = (result.shade, result.sun)
        if not all(math.isfinite(v) for v in values if v is not None):
            raise ArithmeticError(f"non-finite result (shade, sun) = {values}")
        return FeelsLikeData(
            result=result,
            error=None,
            season=season,
            solar_elevation=elevation,
            wind_10m=wind,
            irradiance=ghi,
            irradiance_source=ghi_source,
            albedo=albedo if settings.has_sun else None,
            held_inputs=held,
            backup_inputs=backup,
        )


def holds_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Storage for the last good input values of one helper."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
