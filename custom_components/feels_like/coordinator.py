"""Coordinator: reads input entities and computes feels-like values."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
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
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
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
    DEFAULT_WIND_MOUNTING,
    DOMAIN,
    GROUND_ALBEDO,
    METHOD_SPECS,
    SNOW_ALBEDO,
    SUN_SOURCE_ENTITY_KEYS,
    SUN_SOURCE_ILLUMINANCE,
    SUN_SOURCE_NONE,
    SUN_SOURCE_WEATHER,
    UPDATE_INTERVAL_MINUTES,
    WIND_MOUNTING_CUSTOM,
    WIND_MOUNTING_FACTORS,
)
from .formulas import solar
from .formulas.feels_like import FeelsLikeInputs, FeelsLikeResult, compute
from .formulas.season import meteorological_season

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
HUMIDITY_RANGE = (0.0, 100.0)  # %
MAX_WIND_SPEED = 75.0  # m/s, as measured (before the mounting factor)

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

    def __init__(self, message: str, *, config_problem: bool = False) -> None:
        """Initialise; config_problem marks errors the user must fix."""
        super().__init__(message)
        self.config_problem = config_problem


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

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        """Build settings from config entry options."""
        method = options[CONF_METHOD]
        spec = METHOD_SPECS[method]

        mounting = options.get(CONF_WIND_MOUNTING, DEFAULT_WIND_MOUNTING)
        if mounting == WIND_MOUNTING_CUSTOM:
            wind_factor = float(options.get(CONF_WIND_FACTOR, 1.0))
        else:
            wind_factor = WIND_MOUNTING_FACTORS.get(mounting, 1.0)

        sun_source = (
            options.get(CONF_SUN_SOURCE, SUN_SOURCE_NONE)
            if spec.supports_sun
            else SUN_SOURCE_NONE
        )
        sun_entity_key = SUN_SOURCE_ENTITY_KEYS.get(sun_source)

        return cls(
            method=method,
            temperature_entity=options[CONF_TEMPERATURE_ENTITY],
            humidity_entity=(
                options.get(CONF_HUMIDITY_ENTITY) if spec.needs_humidity else None
            ),
            wind_entity=options.get(CONF_WIND_ENTITY) if spec.needs_wind else None,
            wind_factor=wind_factor,
            sun_source=sun_source,
            sun_entity=options.get(sun_entity_key) if sun_entity_key else None,
            lux_per_wm2=float(options.get(CONF_LUX_PER_WM2, DEFAULT_LUX_PER_WM2)),
            ground_albedo=GROUND_ALBEDO[
                options.get(CONF_GROUND_SURFACE, DEFAULT_GROUND_SURFACE)
            ],
            snow_entity=(
                options.get(CONF_SNOW_ENTITY) if sun_source != SUN_SOURCE_NONE else None
            ),
            min_update_interval=float(
                options.get(CONF_MIN_UPDATE_INTERVAL, DEFAULT_MIN_UPDATE_INTERVAL)
            ),
            hold_time=timedelta(
                seconds=float(options.get(CONF_HOLD_TIME, DEFAULT_HOLD_TIME))
            ),
            backup_weather_entity=options.get(CONF_BACKUP_WEATHER_ENTITY) or None,
        )

    @property
    def tracked_entities(self) -> list[str]:
        """Entities whose state changes trigger a recalculation.

        The backup weather entity is not tracked: it is only read while an input
        is out, and the periodic refresh picks up its changes.
        """
        return [
            entity_id
            for entity_id in (
                self.temperature_entity,
                self.humidity_entity,
                self.wind_entity,
                self.sun_entity,
                self.snow_entity,
            )
            if entity_id
        ]


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


def _level(err: InputError) -> int:
    return logging.WARNING if err.config_problem else logging.DEBUG


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
    return value


def _to_meters_per_second(entity_id: str, value: float, unit: str) -> float:
    if unit not in SpeedConverter.VALID_UNITS:
        raise InputError(
            f"{entity_id} has unsupported unit '{unit}'", config_problem=True
        )
    speed = SpeedConverter.convert(value, unit, UnitOfSpeed.METERS_PER_SECOND)
    if speed > MAX_WIND_SPEED:
        raise InputError(f"{entity_id} has implausible wind speed {speed:g} m/s")
    return max(speed, 0.0)


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

    async def _async_update_data(self) -> FeelsLikeData:
        """Recalculate (on schedule and on throttled input changes)."""
        return self.calculate()

    @callback
    def async_track_inputs(self) -> CALLBACK_TYPE:
        """Recalculate when an input entity changes, throttled by the debouncer."""

        @callback
        def _state_changed(event: Event[EventStateChangedData]) -> None:  # noqa: ARG001
            self._input_debouncer.async_schedule_call()

        return async_track_state_change_event(
            self.hass, self.settings.tracked_entities, _state_changed
        )

    # ------------------------------------------------------------ resolution

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
        level = _level(err)
        last = self._last_good.get(key)
        if last is not None and now - last[1] < self.settings.hold_time:
            status.held.append(entity_id)
            status.report(f"{err}; using its last value", level)
            return last[0], _Origin.HELD

        problem = str(err)
        if last is not None:
            # Out for longer than the hold time: worth telling the user.
            problem = f"{err} for longer than the hold time"
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
            raise InputError(f"{entity_id} does not exist")
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
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UnitOfTemperature.CELSIUS
        )
        return _to_celsius(entity_id, value, unit)

    def _backup_temperature(self, entity_id: str) -> float:
        state = self._available_state(entity_id)
        value = _attribute_value(state, entity_id, ATTR_WEATHER_TEMPERATURE)
        unit = (
            state.attributes.get(ATTR_WEATHER_TEMPERATURE_UNIT)
            or self.hass.config.units.temperature_unit
        )
        return _to_celsius(entity_id, value, unit)

    def _read_humidity(self, entity_id: str) -> float:
        value, state = self._numeric_state(entity_id)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
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
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            or UnitOfSpeed.METERS_PER_SECOND
        )
        speed = _to_meters_per_second(entity_id, value, unit)
        return speed * self.settings.wind_factor

    def _backup_wind(self, entity_id: str) -> float:
        """Wind at 10 m from a weather entity (forecasts are already at 10 m)."""
        state = self._available_state(entity_id)
        value = _attribute_value(state, entity_id, ATTR_WEATHER_WIND_SPEED)
        unit = (
            state.attributes.get(ATTR_WEATHER_WIND_SPEED_UNIT)
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
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if source == SUN_SOURCE_ILLUMINANCE:
            if unit not in (None, LIGHT_LUX):
                raise InputError(
                    f"{entity_id} has unsupported unit '{unit}'", config_problem=True
                )
            return solar.lux_to_irradiance(value, self.settings.lux_per_wm2)

        if unit == UnitOfIrradiance.BTUS_PER_HOUR_SQUARE_FOOT:
            value *= BTU_PER_HOUR_SQFT_TO_W_PER_M2
        elif unit not in (None, UnitOfIrradiance.WATTS_PER_SQUARE_METER):
            raise InputError(
                f"{entity_id} has unsupported unit '{unit}'", config_problem=True
            )
        return max(value, 0.0)

    def _read_snow_albedo(self, entity_id: str) -> float:
        state = self._available_state(entity_id)
        if state.state == STATE_ON:
            return SNOW_ALBEDO
        try:
            depth = float(state.state)
        except ValueError:
            return self.settings.ground_albedo  # "off" or another non-snow state
        return SNOW_ALBEDO if depth > 0 else self.settings.ground_albedo

    # ------------------------------------------------------------ optional inputs

    def _irradiance(
        self, elevation: float, now: datetime, status: _Status
    ) -> tuple[float | None, str | None]:
        """Return (global horizontal irradiance, source) or (None, None)."""
        source = self.settings.sun_source
        entity_id = self.settings.sun_entity
        if source == SUN_SOURCE_NONE or entity_id is None:
            return None, None

        try:
            raw = self._read_live(_SUN, entity_id, self._read_sun, now)
            origin = _Origin.LIVE
        except InputError as err:
            if elevation <= 0:
                # No sunlight anyway: a missing sun input does not matter.
                status.report(f"{err} (not needed while the sun is down)", _level(err))
                return 0.0, IRRADIANCE_SOURCE_NIGHT
            try:
                raw, origin = self._substitute(
                    _SUN, entity_id, err, self._cloud_coverage, now, status
                )
            except InputError:
                return None, None

        # Cloud cover is held rather than irradiance, so estimates follow the sun.
        if source == SUN_SOURCE_WEATHER or origin is _Origin.BACKUP:
            clear = solar.haurwitz_clear_sky(elevation)
            return solar.kasten_czeplak(clear, raw), IRRADIANCE_SOURCE_ESTIMATED
        if source == SUN_SOURCE_ILLUMINANCE:
            return raw, IRRADIANCE_SOURCE_ILLUMINANCE
        return raw, IRRADIANCE_SOURCE_SENSOR

    def _albedo(self, now: datetime, status: _Status) -> float:
        entity_id = self.settings.snow_entity
        if not entity_id:
            return self.settings.ground_albedo
        try:
            albedo, _origin = self._resolve(
                _SNOW, entity_id, self._read_snow_albedo, None, now, status
            )
        except InputError:
            return self.settings.ground_albedo
        return albedo

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
        return FeelsLikeData(
            result=result,
            error=None,
            season=season,
            solar_elevation=elevation,
            wind_10m=wind,
            irradiance=ghi,
            irradiance_source=ghi_source,
            albedo=albedo if settings.sun_source != SUN_SOURCE_NONE else None,
            held_inputs=held,
            backup_inputs=backup,
        )
