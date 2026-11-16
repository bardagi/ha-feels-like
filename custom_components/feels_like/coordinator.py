"""Coordinator: reads input entities and computes feels-like values."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    CONF_GROUND_SURFACE,
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
    DEFAULT_LUX_PER_WM2,
    DEFAULT_MIN_UPDATE_INTERVAL,
    DEFAULT_WIND_MOUNTING,
    DOMAIN,
    GROUND_ALBEDO,
    METHOD_SPECS,
    SNOW_ALBEDO,
    SUN_SOURCE_ENTITY_KEYS,
    SUN_SOURCE_ILLUMINANCE,
    SUN_SOURCE_IRRADIANCE,
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

ATTR_CLOUD_COVERAGE = "cloud_coverage"
BTU_PER_HOUR_SQFT_TO_W_PER_M2 = 3.15459
_UNAVAILABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


class InputError(Exception):
    """An input entity is missing, unavailable or has an unsupported value."""

    def __init__(self, message: str, *, config_problem: bool = False) -> None:
        """Initialise; config_problem marks errors the user must fix."""
        super().__init__(message)
        self.config_problem = config_problem


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
        )

    @property
    def tracked_entities(self) -> list[str]:
        """Entities whose state changes trigger a recalculation."""
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


def _state_value(state: State | None, entity_id: str) -> float:
    if state is None:
        raise InputError(f"{entity_id} does not exist")
    if state.state in _UNAVAILABLE_STATES:
        raise InputError(f"{entity_id} is {state.state}")
    try:
        value = float(state.state)
    except ValueError as err:
        raise InputError(
            f"{entity_id} has non-numeric state '{state.state}'", config_problem=True
        ) from err
    if not math.isfinite(value):
        raise InputError(f"{entity_id} is {state.state}")
    return value


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
        self._last_problem_warned = False

    async def _async_update_data(self) -> FeelsLikeData:
        """Recalculate (on schedule and on throttled input changes)."""
        return self.calculate()

    @callback
    def async_track_inputs(self) -> CALLBACK_TYPE:
        """Recalculate when an input entity changes, throttled by the debouncer."""

        @callback
        def _state_changed(event: Event[EventStateChangedData]) -> None:
            self._input_debouncer.async_schedule_call()

        return async_track_state_change_event(
            self.hass, self.settings.tracked_entities, _state_changed
        )

    # ------------------------------------------------------------------ inputs

    def _temperature(self) -> float:
        entity_id = self.settings.temperature_entity
        state = self.hass.states.get(entity_id)
        value = _state_value(state, entity_id)
        assert state is not None
        unit = (
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UnitOfTemperature.CELSIUS
        )
        if unit not in TemperatureConverter.VALID_UNITS:
            raise InputError(
                f"{entity_id} has unsupported unit '{unit}'", config_problem=True
            )
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

    def _humidity(self) -> float | None:
        entity_id = self.settings.humidity_entity
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        value = _state_value(state, entity_id)
        assert state is not None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit not in (None, PERCENTAGE):
            raise InputError(
                f"{entity_id} has unsupported unit '{unit}'", config_problem=True
            )
        return value

    def _wind_10m(self) -> float | None:
        entity_id = self.settings.wind_entity
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        value = _state_value(state, entity_id)
        assert state is not None
        unit = (
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            or UnitOfSpeed.METERS_PER_SECOND
        )
        if unit not in SpeedConverter.VALID_UNITS:
            raise InputError(
                f"{entity_id} has unsupported unit '{unit}'", config_problem=True
            )
        wind = SpeedConverter.convert(value, unit, UnitOfSpeed.METERS_PER_SECOND)
        return max(wind, 0.0) * self.settings.wind_factor

    def _irradiance(self, elevation: float) -> tuple[float | None, str | None]:
        """Return (global horizontal irradiance, source) or (None, reason)."""
        source = self.settings.sun_source
        if source == SUN_SOURCE_NONE:
            return None, None
        entity_id = self.settings.sun_entity
        if entity_id is None:
            return None, None
        state = self.hass.states.get(entity_id)

        try:
            if source == SUN_SOURCE_IRRADIANCE:
                value = _state_value(state, entity_id)
                assert state is not None
                unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                if unit == UnitOfIrradiance.BTUS_PER_HOUR_SQUARE_FOOT:
                    value *= BTU_PER_HOUR_SQFT_TO_W_PER_M2
                elif unit not in (None, UnitOfIrradiance.WATTS_PER_SQUARE_METER):
                    raise InputError(
                        f"{entity_id} has unsupported unit '{unit}'",
                        config_problem=True,
                    )
                return max(value, 0.0), "sensor"

            if source == SUN_SOURCE_ILLUMINANCE:
                value = _state_value(state, entity_id)
                assert state is not None
                unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                if unit not in (None, LIGHT_LUX):
                    raise InputError(
                        f"{entity_id} has unsupported unit '{unit}'",
                        config_problem=True,
                    )
                return solar.lux_to_irradiance(value, self.settings.lux_per_wm2), (
                    "illuminance"
                )

            if source == SUN_SOURCE_WEATHER:
                if state is None or state.state in _UNAVAILABLE_STATES:
                    raise InputError(f"{entity_id} is unavailable")
                cloud = state.attributes.get(ATTR_CLOUD_COVERAGE)
                if cloud is None:
                    raise InputError(
                        f"{entity_id} has no cloud_coverage attribute",
                        config_problem=True,
                    )
                try:
                    cloud_pct = float(cloud)
                except (TypeError, ValueError):
                    cloud_pct = math.nan
                if not math.isfinite(cloud_pct):
                    raise InputError(
                        f"{entity_id} has non-numeric cloud_coverage '{cloud}'",
                        config_problem=True,
                    )
                clear = solar.haurwitz_clear_sky(elevation)
                return solar.kasten_czeplak(clear, cloud_pct), "estimated"
        except InputError as err:
            self._report_problem(f"sun input: {err}", err.config_problem)
            return None, None
        return None, None

    def _albedo(self) -> float:
        albedo = self.settings.ground_albedo
        entity_id = self.settings.snow_entity
        if not entity_id:
            return albedo
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            return albedo
        if state.state == STATE_ON:
            return SNOW_ALBEDO
        try:
            return SNOW_ALBEDO if float(state.state) > 0 else albedo
        except ValueError:
            return albedo

    # ------------------------------------------------------------- calculation

    def _report_problem(
        self, problem: str | None, config_problem: bool = False
    ) -> None:
        """Log input problems once per change instead of on every update."""
        if problem == self._last_problem:
            return
        title = self.config_entry.title
        if problem is None:
            # Transient unavailability (e.g. at startup) recovers silently;
            # only announce recovery from problems we warned about.
            level = logging.INFO if self._last_problem_warned else logging.DEBUG
            _LOGGER.log(level, "%s: all inputs available again", title)
        elif config_problem:
            _LOGGER.warning("%s: %s", title, problem)
        else:
            _LOGGER.debug("%s: %s", title, problem)
        self._last_problem = problem
        self._last_problem_warned = problem is not None and config_problem

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

        try:
            temperature = self._temperature()
            humidity = self._humidity()
            wind = self._wind_10m()
        except InputError as err:
            self._report_problem(str(err), err.config_problem)
            return FeelsLikeData(
                result=None,
                error=str(err),
                season=season,
                solar_elevation=elevation,
            )

        ghi, ghi_source = self._irradiance(elevation)
        sun_ok = ghi is not None or self.settings.sun_source == SUN_SOURCE_NONE
        if sun_ok and self._last_problem is not None:
            self._report_problem(None)
        albedo = self._albedo()

        result = compute(
            FeelsLikeInputs(
                method=self.settings.method,
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
            albedo=albedo if self.settings.sun_source != SUN_SOURCE_NONE else None,
        )
