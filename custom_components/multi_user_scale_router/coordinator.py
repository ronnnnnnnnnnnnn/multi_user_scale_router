"""Runtime orchestration for the router integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import MassConverter

from .const import (
    CONF_HISTORY_RETENTION_DAYS,
    CONF_MAX_HISTORY_SIZE,
    CONF_MOBILE_NOTIFY_SERVICES,
    CONF_MIN_TOLERANCE_KG,
    CONF_PERSON_ENTITY,
    CONF_SOURCE_ENTITY_ID,
    CONF_ROUTER_STATE,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_MAX_HISTORY_SIZE,
    DEFAULT_MIN_TOLERANCE_KG,
    DOMAIN,
    MAX_PENDING_MEASUREMENTS,
)
from multi_user_scale_core import (
    MeasurementCandidate,
    MeasurementNotFoundError,
    RouterConfig,
    UserProfile,
    WeightMeasurement,
    WeightRouter,
)

_LOGGER = logging.getLogger(__name__)


def _norm_country_code(config: Any) -> str:
    """Extract normalized 2-letter country code from HA config."""
    raw = (getattr(config, "country", None) or "").upper()
    return raw[:2] if len(raw) >= 2 else ""


@dataclass
class PendingMeasurement:
    """Ambiguous measurement held in integration runtime state."""

    measurement: WeightMeasurement
    candidate_details: list[dict[str, Any]]
    created_at: datetime
    notified_mobile_services: list[tuple[str, str]] = field(default_factory=list)


def _safe_int(value: Any, default: int, field_name: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid integer")
        return int(value)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Invalid integer config value for %s: %s, using default %s",
            field_name,
            value,
            default,
        )
        return default


def _safe_float_config(value: Any, default: float, field_name: str) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid float")
        return float(value)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Invalid float config value for %s: %s, using default %s",
            field_name,
            value,
            default,
        )
        return default


def _safe_config_users(raw_users: Any) -> list[UserProfile]:
    if not isinstance(raw_users, list):
        _LOGGER.warning(
            "Invalid users config payload type %s, using empty user list",
            type(raw_users),
        )
        return []

    users: list[UserProfile] = []
    for item in raw_users:
        try:
            users.append(UserProfile.from_dict(item))
        except (TypeError, KeyError, ValueError) as error:
            _LOGGER.warning("Invalid user profile payload ignored: %s", error)
    return users


def _safe_user_config_by_id(raw_users: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_users, list):
        return {}

    config_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_users:
        if not isinstance(item, dict):
            continue
        user_id = item.get("user_id")
        if not isinstance(user_id, str):
            continue
        config_by_id[user_id] = item
    return config_by_id


def _convert_to_kg(value: float, unit: str) -> float | None:
    normalized = (unit or "").lower().strip()
    if normalized in {"kg", "kilogram", "kilograms"}:
        return value
    if normalized in {"lb", "lbs", "pound", "pounds"}:
        return value * 0.45359237
    return None


def _normalize_display_unit(unit: str | None) -> str:
    normalized = (unit or "").lower().strip()
    if normalized in {"lb", "lbs", "pound", "pounds"}:
        return "lb"
    return "kg"


def _convert_from_kg(value_kg: float, display_unit: str) -> float:
    if _normalize_display_unit(display_unit) == "lb":
        return MassConverter.convert(value_kg, UnitOfMass.KILOGRAMS, UnitOfMass.POUNDS)
    return value_kg


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RouterRuntime:
    """Hold per-entry runtime state and connect core logic to HA callbacks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.entry_data = entry.data
        self.listeners: set[Callable[[], None]] = set()
        self.diagnostic_listeners: set[Callable[[], None]] = set()
        self._unsub_state: Callable[[], None] | None = None
        self._router_state_recovered = True
        self._pending_measurements: dict[str, PendingMeasurement] = {}
        self._user_config_by_id = _safe_user_config_by_id(
            self.entry_data.get("users", [])
        )

        users = _safe_config_users(self.entry_data.get("users", []))
        config = RouterConfig(
            history_retention_days=_safe_int(
                self.entry_data.get(
                    CONF_HISTORY_RETENTION_DAYS,
                    DEFAULT_HISTORY_RETENTION_DAYS,
                ),
                DEFAULT_HISTORY_RETENTION_DAYS,
                CONF_HISTORY_RETENTION_DAYS,
            ),
            max_history_size=_safe_int(
                self.entry_data.get(
                    CONF_MAX_HISTORY_SIZE,
                    DEFAULT_MAX_HISTORY_SIZE,
                ),
                DEFAULT_MAX_HISTORY_SIZE,
                CONF_MAX_HISTORY_SIZE,
            ),
            min_tolerance_kg=_safe_float_config(
                self.entry_data.get(
                    CONF_MIN_TOLERANCE_KG,
                    DEFAULT_MIN_TOLERANCE_KG,
                ),
                DEFAULT_MIN_TOLERANCE_KG,
                CONF_MIN_TOLERANCE_KG,
            ),
        )

        router_state = self.entry_data.get(CONF_ROUTER_STATE)
        if isinstance(router_state, dict):
            try:
                self.router = WeightRouter.from_dict(router_state)
                self.router.set_config(config)
                self.router.set_users(users)
            except (TypeError, ValueError, KeyError, AttributeError) as error:
                self._router_state_recovered = False
                self.router = WeightRouter(config=config)
                self.router.set_users(users)
                _LOGGER.warning(
                    "Failed to restore router state from config entry '%s': %s",
                    self.entry_id,
                    error,
                )
        elif router_state is not None:
            self._router_state_recovered = False
            self.router = WeightRouter(config=config)
            self.router.set_users(users)
            _LOGGER.warning(
                "Invalid router state type for config entry '%s': expected dictionary, got %s",
                self.entry_id,
                type(router_state).__name__,
            )
        else:
            self.router = WeightRouter(config=config)
            self.router.set_users(users)

        self.source_entity_id = self.entry_data[CONF_SOURCE_ENTITY_ID]

    @property
    def title(self) -> str:
        return self.entry.title or f"Multi-User Scale Router {self.entry_id}"

    @property
    def device_id(self) -> str | None:
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, self.entry_id)}
        )
        return device_entry.id if device_entry is not None else None

    @property
    def source_entity(self) -> str:
        return self.source_entity_id

    @property
    def display_unit(self) -> str:
        state = getattr(self.hass, "states", None)
        if state is not None:
            source_state = state.get(self.source_entity_id)
            source_unit = getattr(source_state, "attributes", {}).get(
                "unit_of_measurement"
            )
            if isinstance(source_unit, str) and source_unit:
                return _normalize_display_unit(source_unit)

        for pending in self.pending_measurements:
            if pending.measurement.source_unit:
                return _normalize_display_unit(pending.measurement.source_unit)

        for user in self.users:
            last_measurement = self.router.get_user_last_measurement(user.user_id)
            if last_measurement is not None and last_measurement.source_unit:
                return _normalize_display_unit(last_measurement.source_unit)

        return "kg"

    @property
    def users(self) -> list[UserProfile]:
        return self.router.get_users()

    @property
    def pending_count(self) -> int:
        return len(self._pending_measurements)

    @property
    def last_measurement_timestamps(self) -> list[str]:
        return [
            entry.measurement.timestamp.isoformat()
            for entry in self.pending_measurements
        ][-10:]

    @property
    def pending_measurements(self) -> list[PendingMeasurement]:
        return sorted(
            self._pending_measurements.values(),
            key=lambda entry: entry.created_at,
            reverse=True,
        )

    @property
    def pending_measurement_details(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for entry in self.pending_measurements:
            pending.append(
                {
                    "measurement_id": entry.measurement.measurement_id,
                    "timestamp": entry.measurement.timestamp.isoformat(),
                    "weight": round(
                        self.display_weight_value(entry.measurement.weight_kg), 2
                    ),
                    "unit_of_measurement": self.display_unit,
                    "weight_kg": round(entry.measurement.weight_kg, 2),
                    "source_entity_id": entry.measurement.source_id,
                    "source_unit": entry.measurement.source_unit,
                }
            )
        return pending

    @property
    def user_directory(self) -> list[dict[str, Any]]:
        directory: list[dict[str, Any]] = []
        for user in self.users:
            last_measurement = self.router.get_user_last_measurement(user.user_id)
            history = self.router.get_user_history(user.user_id)
            user_config = self._user_config_by_id.get(user.user_id, {})
            directory.append(
                {
                    "user_id": user.user_id,
                    "display_name": user.display_name,
                    "person_entity": user_config.get(CONF_PERSON_ENTITY),
                    "history_count": len(history),
                    "last_measurement_id": (
                        last_measurement.measurement_id
                        if last_measurement is not None
                        else None
                    ),
                    "last_weight_kg": (
                        round(last_measurement.weight_kg, 2)
                        if last_measurement is not None
                        else None
                    ),
                    "last_weight": (
                        round(self.display_weight_value(last_measurement.weight_kg), 2)
                        if last_measurement is not None
                        else None
                    ),
                    "unit_of_measurement": self.display_unit,
                    "last_timestamp": (
                        last_measurement.timestamp.isoformat()
                        if last_measurement is not None
                        else None
                    ),
                }
            )
        return directory

    def add_listener(self, update_callback: Callable[[], None]) -> None:
        self.listeners.add(update_callback)

    def remove_listener(self, update_callback: Callable[[], None]) -> None:
        self.listeners.discard(update_callback)

    def add_diagnostic_listener(self, update_callback: Callable[[], None]) -> None:
        self.diagnostic_listeners.add(update_callback)

    def remove_diagnostic_listener(self, update_callback: Callable[[], None]) -> None:
        self.diagnostic_listeners.discard(update_callback)

    @callback
    def _notify(self) -> None:
        for listener in list(self.listeners):
            self.hass.add_job(listener)

    @callback
    def _notify_diagnostic_sensors(self) -> None:
        for listener in list(self.diagnostic_listeners):
            self.hass.add_job(listener)

    def async_setup(self) -> None:
        if self._unsub_state:
            return
        if not self._router_state_recovered:
            self.persist_router_state()
        self._unsub_state = async_track_state_change_event(
            self.hass,
            self.source_entity_id,
            self._async_handle_source_update,
        )

    def async_unload(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        for measurement_id in list(self._pending_measurements):
            pending = self._pending_measurements.get(measurement_id)
            if (
                pending is not None
                and pending.notified_mobile_services
                and hasattr(self.hass, "async_create_task")
            ):
                self.hass.async_create_task(
                    self._clear_mobile_notifications(
                        measurement_id,
                        pending.notified_mobile_services,
                    )
                )
            self._dismiss_pending_notification(measurement_id)
        self._pending_measurements.clear()
        self.listeners.clear()
        self.diagnostic_listeners.clear()

    def persist_router_state(self) -> None:
        payload = dict(self.entry.data)
        payload[CONF_ROUTER_STATE] = self.router.to_dict()
        try:
            self.hass.config_entries.async_update_entry(self.entry, data=payload)
        except Exception as error:
            _LOGGER.warning(
                "Failed to persist router state for entry '%s': %s",
                self.entry_id,
                error,
            )

    def get_user_display_name(self, user_id: str) -> str:
        for user in self.users:
            if user.user_id == user_id:
                return user.display_name
        return user_id

    def get_user_person_entity(self, user_id: str) -> str | None:
        user_config = self._user_config_by_id.get(user_id, {})
        person_entity = user_config.get(CONF_PERSON_ENTITY)
        return str(person_entity) if isinstance(person_entity, str) else None

    def display_weight_value(self, value_kg: float) -> float:
        return _convert_from_kg(value_kg, self.display_unit)

    def format_weight(self, value_kg: float, precision: int = 2) -> str:
        value = self.display_weight_value(value_kg)
        return f"{value:.{precision}f} {self.display_unit}"

    # Countries that typically use 12-hour time (HA backend does not expose
    # per-user time format; use country as heuristic).
    _COUNTRY_12H: frozenset[str] = frozenset({"US", "CA", "PH", "AU"})

    def _get_display_preferences(self) -> tuple[str, str | None, str | None]:
        """Return (language, time_format, date_format) from HA config.

        time_format: '12' | '24' | None. If not set on config, inferred from
            hass.config.country (12h for US, CA, PH, AU; 24h otherwise).
        date_format: 'dmy' | 'mdy' | 'ymd' | None only when explicitly set;
            we do not infer from country so that we can use an unambiguous
            spelled-out date (e.g. "Mar 8, 2026") when date_format is None.
        """
        config = getattr(self.hass, "config", None)
        if config is None:
            return "en", None, None
        language = getattr(config, "language", None) or "en"
        time_fmt = getattr(config, "time_format", None)
        if time_fmt in ("language", "auto", ""):
            time_fmt = None
        date_fmt = getattr(config, "date_format", None)
        if date_fmt in ("language", "auto", ""):
            date_fmt = None
        country = _norm_country_code(config)
        if time_fmt is None and country:
            time_fmt = "12" if country in self._COUNTRY_12H else "24"
        return language, time_fmt, date_fmt

    def _format_time_part(
        self, localized: datetime, time_format: str | None, include_seconds: bool
    ) -> str | None:
        """Format time part from display preferences. Returns None if Babel should be used."""
        if time_format == "24":
            fmt = "%H:%M:%S" if include_seconds else "%H:%M"
            return localized.strftime(fmt)
        if time_format == "12":
            fmt = "%I:%M:%S %p" if include_seconds else "%I:%M %p"
            return localized.strftime(fmt).lstrip("0")
        return None

    def _format_date_unambiguous(self, localized: datetime, language: str) -> str:
        """Format date with spelled month (e.g. Mar 8, 2026) for clarity."""
        try:
            from babel.dates import format_date as babel_format_date

            return babel_format_date(
                localized,
                format="medium",
                locale=language.replace("-", "_"),
            )
        except Exception as err:
            _LOGGER.debug(
                "Babel format_date failed (locale=%s), using strftime fallback: %s",
                language,
                err,
            )
            return localized.strftime("%b %d, %Y")

    def _format_notification_timestamp(self, value: datetime) -> str:
        localized = dt_util.as_local(value)
        language, time_format, date_format = self._get_display_preferences()
        if date_format is not None:
            date_patterns = {
                "dmy": "%d/%m/%Y",
                "mdy": "%m/%d/%Y",
                "ymd": "%Y-%m-%d",
            }
            date_part = date_patterns.get((date_format or "").lower(), "%b %d, %Y")
            time_fmt = "%H:%M:%S" if time_format == "24" else "%I:%M:%S %p"
            return localized.strftime(f"{date_part} at {time_fmt} %Z")
        time_str = self._format_time_part(localized, time_format, True)
        if time_str is None:
            time_str = localized.strftime("%I:%M:%S %p").lstrip("0")
        date_str = self._format_date_unambiguous(localized, language)
        return f"{date_str} at {time_str} {localized.strftime('%Z')}"

    def _format_notification_time(self, value: datetime) -> str:
        localized = dt_util.as_local(value)
        language, time_format, _date_fmt = self._get_display_preferences()
        time_str = self._format_time_part(localized, time_format, False)
        if time_str is not None:
            return time_str
        try:
            from babel.dates import format_datetime as babel_format_datetime

            return babel_format_datetime(
                localized, format="short", locale=language.replace("-", "_")
            )
        except Exception as err:
            _LOGGER.debug(
                "Babel format_datetime failed (locale=%s), using strftime fallback: %s",
                language,
                err,
            )
            return localized.strftime("%I:%M %p").lstrip("0")

    @property
    def last_user_weight_by_id(self) -> dict[str, float]:
        values = {}
        for user in self.users:
            last_measurement = self.router.get_user_last_measurement(user.user_id)
            if last_measurement is not None:
                values[user.user_id] = last_measurement.weight_kg
        return values

    def _filter_user_ids_by_location(self, user_ids: list[str]) -> list[str]:
        states = getattr(self.hass, "states", None)
        if states is None:
            return list(user_ids)

        filtered_user_ids: list[str] = []
        for user_id in user_ids:
            user_config = self._user_config_by_id.get(user_id, {})
            person_entity_id = user_config.get(CONF_PERSON_ENTITY)
            if not person_entity_id:
                filtered_user_ids.append(user_id)
                continue

            person_state = states.get(person_entity_id)
            if not person_state:
                filtered_user_ids.append(user_id)
                continue

            if str(person_state.state).lower() == "not_home":
                continue

            filtered_user_ids.append(user_id)

        if not filtered_user_ids and user_ids:
            return list(user_ids)

        return filtered_user_ids

    def _resolve_candidate_user_ids(
        self,
        measurement: WeightMeasurement,
        candidates: list[MeasurementCandidate],
    ) -> list[str]:
        candidate_user_ids = [candidate.user_id for candidate in candidates]
        filtered_candidates = self._filter_user_ids_by_location(candidate_user_ids)
        if filtered_candidates:
            return filtered_candidates
        if candidate_user_ids:
            return candidate_user_ids

        all_user_ids = [user.user_id for user in self.users]
        if not all_user_ids:
            return []

        filtered_all_users = self._filter_user_ids_by_location(all_user_ids)
        if filtered_all_users:
            return filtered_all_users

        _LOGGER.debug(
            "No routed candidates found for measurement %s, falling back to all configured users",
            measurement.measurement_id,
        )
        return all_user_ids

    def _store_pending_measurement(
        self,
        measurement: WeightMeasurement,
        candidates: list[MeasurementCandidate],
        candidate_user_ids: list[str],
    ) -> None:
        candidates_by_user_id = {
            candidate.user_id: candidate for candidate in candidates
        }
        candidate_details: list[dict[str, Any]] = []
        for user_id in candidate_user_ids:
            candidate = candidates_by_user_id.get(user_id)
            candidate_details.append(
                {
                    "user_id": user_id,
                    "display_name": self.get_user_display_name(user_id),
                    "person_entity": self.get_user_person_entity(user_id),
                    "unit_of_measurement": self.display_unit,
                    "reference_weight_kg": (
                        round(candidate.reference_weight_kg, 2)
                        if candidate is not None
                        and candidate.reference_weight_kg is not None
                        else None
                    ),
                    "reference_weight": (
                        round(
                            self.display_weight_value(candidate.reference_weight_kg), 2
                        )
                        if candidate is not None
                        and candidate.reference_weight_kg is not None
                        else None
                    ),
                    "tolerance_kg": (
                        round(candidate.tolerance_kg, 2)
                        if candidate is not None and candidate.tolerance_kg is not None
                        else None
                    ),
                    "tolerance": (
                        round(self.display_weight_value(candidate.tolerance_kg), 2)
                        if candidate is not None and candidate.tolerance_kg is not None
                        else None
                    ),
                }
            )
        self._pending_measurements[measurement.measurement_id] = PendingMeasurement(
            measurement=measurement,
            candidate_details=candidate_details,
            created_at=measurement.timestamp,
        )
        self._create_pending_notification(measurement.measurement_id)
        if hasattr(self.hass, "async_create_task"):
            self.hass.async_create_task(
                self._send_mobile_notifications_for_pending_measurement(
                    measurement.measurement_id
                )
            )
        while len(self._pending_measurements) > MAX_PENDING_MEASUREMENTS:
            oldest_measurement_id = next(iter(self._pending_measurements))
            removed = self._pending_measurements.pop(oldest_measurement_id, None)
            if (
                removed is not None
                and removed.notified_mobile_services
                and hasattr(self.hass, "async_create_task")
            ):
                self.hass.async_create_task(
                    self._clear_mobile_notifications(
                        oldest_measurement_id,
                        removed.notified_mobile_services,
                    )
                )
            self._dismiss_pending_notification(oldest_measurement_id)
        self._notify_diagnostic_sensors()

    def _notification_id(self, measurement_id: str) -> str:
        return f"{self.entry_id}_pending_{measurement_id}"

    def _notification_tag(self, measurement_id: str) -> str:
        return f"scale_router_measurement_{measurement_id}"

    async def _send_mobile_notifications_for_pending_measurement(
        self, measurement_id: str
    ) -> None:
        pending = self._pending_measurements.get(measurement_id)
        if pending is None:
            return

        hass_services = getattr(self.hass, "services", None)
        if hass_services is None or not hasattr(hass_services, "async_call"):
            return

        tag = self._notification_tag(measurement_id)
        service_to_users: dict[str, list[tuple[str, str]]] = {}
        for candidate in pending.candidate_details:
            user_id = candidate["user_id"]
            display_name = candidate["display_name"]
            user_config = self._user_config_by_id.get(user_id, {})
            mobile_services = user_config.get(CONF_MOBILE_NOTIFY_SERVICES, [])
            if not isinstance(mobile_services, list):
                continue
            for service_name in mobile_services:
                if not isinstance(service_name, str) or not service_name:
                    continue
                service_to_users.setdefault(service_name, []).append(
                    (user_id, display_name)
                )

        notified_services: list[tuple[str, str]] = []
        time_display = self._format_notification_time(pending.measurement.timestamp)
        action_prefix = "ROUTER_ASSIGN_"
        action_not_me_prefix = "ROUTER_NOT_ME_"
        action_entry = quote(self.entry_id, safe="")
        action_measurement = quote(pending.measurement.measurement_id, safe="")
        weight_display = self.format_weight(pending.measurement.weight_kg, 1)
        for service_name, users in service_to_users.items():
            if measurement_id not in self._pending_measurements:
                return

            action_data: dict[str, Any]

            if len(users) == 1:
                user_id, user_name = users[0]
                shared_user_names: list[str] = []
                for user in self.users:
                    if user.user_id == user_id:
                        continue
                    profile = self._user_config_by_id.get(user.user_id, {})
                    service_list = profile.get(CONF_MOBILE_NOTIFY_SERVICES, [])
                    if not isinstance(service_list, list):
                        continue
                    if service_name in service_list:
                        shared_user_names.append(user.display_name)

                if shared_user_names:
                    message = (
                        f"{weight_display} at {time_display}. "
                        f"Is this {user_name}'s?"
                    )
                    assign_title = f"Assign to {user_name}"
                    not_me_title = f"Not {user_name}"
                else:
                    message = f"{weight_display} at {time_display}. " f"Is this yours?"
                    assign_title = "Assign to Me"
                    not_me_title = "Not Me"

                action_data = {
                    "measurement_id": pending.measurement.measurement_id,
                    "user_id": user_id,
                }
            else:
                names = ", ".join(name for _, name in users)
                message = (
                    f"{weight_display} at {time_display}. " f"Who stepped on? ({names})"
                )
                if len(users) > 3:
                    overflow = ", ".join(name for _, name in users[3:])
                    message += (
                        f" (Tap for {', '.join(name for _, name in users[:3])}, "
                        f"+{len(users) - 3} more: {overflow})"
                    )
                action_data = {
                    "measurement_id": pending.measurement.measurement_id,
                    "user_ids": [user_id for user_id, _ in users],
                }

            actions = []
            for index, (user_id, user_name) in enumerate(users):
                if not user_id:
                    continue
                action_user = quote(user_id, safe="")

                if index == 0 and len(users) == 1:
                    assign_title = assign_title
                else:
                    assign_title = f"Assign to {user_name}"

                actions.append(
                    {
                        "action": (
                            f"{action_prefix}"
                            f"{action_entry}|{action_measurement}|{action_user}"
                        ),
                        "title": assign_title,
                    }
                )
                if len(users) == 1:
                    actions.append(
                        {
                            "action": (
                                f"{action_not_me_prefix}"
                                f"{action_entry}|{action_measurement}|{action_user}"
                            ),
                            "title": not_me_title,
                        }
                    )
                elif len(users) <= 3:
                    actions.append(
                        {
                            "action": (
                                f"{action_not_me_prefix}"
                                f"{action_entry}|{action_measurement}|{action_user}"
                            ),
                            "title": f"Not {user_name}",
                        }
                    )
                else:
                    actions.append(
                        {
                            "action": (
                                f"{action_not_me_prefix}"
                                f"{action_entry}|{action_measurement}|{action_user}"
                            ),
                            "title": "Not Me",
                        }
                    )

            try:
                await self.hass.services.async_call(
                    "notify",
                    service_name,
                    {
                        "title": "❓ Unassigned Scale Measurement",
                        "message": message,
                        "data": {
                            "tag": tag,
                            "group": "scale-measurements",
                            "channel": "Scale Measurements",
                            "importance": "default",
                            "actions": actions,
                            "action_data": action_data,
                        },
                    },
                )
                for user_id, _ in users:
                    notified_services.append((user_id, service_name))
            except Exception as error:
                _LOGGER.debug(
                    "Failed sending mobile notification via %s: %s",
                    service_name,
                    error,
                )

        pending = self._pending_measurements.get(measurement_id)
        if pending is not None:
            pending.notified_mobile_services = notified_services

    async def _clear_mobile_notifications(
        self,
        measurement_id: str,
        notified_services: list[tuple[str, str]],
    ) -> None:
        if not notified_services:
            return

        hass_services = getattr(self.hass, "services", None)
        if hass_services is None or not hasattr(hass_services, "async_call"):
            return

        tag = self._notification_tag(measurement_id)
        for _user_id, service_name in notified_services:
            try:
                await self.hass.services.async_call(
                    "notify",
                    service_name,
                    {"message": "clear_notification", "data": {"tag": tag}},
                )
            except Exception as error:
                _LOGGER.debug(
                    "Failed clearing mobile notification via %s: %s",
                    service_name,
                    error,
                )

    def _create_pending_notification(self, measurement_id: str) -> None:
        pending = self._pending_measurements.get(measurement_id)
        if pending is None:
            return
        device_id = self.device_id or "DEVICE_ID"

        candidate_lines: list[str] = []
        for candidate in pending.candidate_details:
            candidate_lines.append(
                f"- **{candidate['display_name']}** ({candidate['user_id']})"
            )

        timestamp_display = self._format_notification_timestamp(
            pending.measurement.timestamp
        )

        message = (
            f"**Scale Router: {self.title}**\n\n"
            f"**Multiple users could match this measurement.**\n\n"
            f"Weight: **{self.format_weight(pending.measurement.weight_kg)}**\n"
            f"Measurement ID: `{pending.measurement.measurement_id}`\n"
            f"Timestamp: `{timestamp_display}`\n\n"
            f"**Candidates:**\n"
            f"{chr(10).join(candidate_lines) if candidate_lines else '- No user suggestions available.'}\n\n"
            f"**To assign this measurement:**\n"
            f"1. Copy the action call below\n"
            f"2. Go to **Developer Tools → Actions**\n"
            f"3. Paste and choose the correct `user_id`\n"
            f"4. Click **Perform Action**\n\n"
            f"```yaml\n"
            f"action: multi_user_scale_router.assign_measurement\n"
            f"data:\n"
            f"  device_id: {device_id}\n"
            f"  measurement_id: {pending.measurement.measurement_id}\n"
            f'  user_id: <SELECT_USER_ID_FROM_ABOVE>\n'
            f"```\n\n"
            f"This notification will auto-dismiss once the measurement is assigned."
        )
        persistent_notification.create(
            self.hass,
            message,
            title=f"{self.title}: Choose User",
            notification_id=self._notification_id(measurement_id),
        )

    def _dismiss_pending_notification(self, measurement_id: str) -> None:
        persistent_notification.dismiss(
            self.hass,
            self._notification_id(measurement_id),
        )

    def record_measurement_for_user(
        self, user_id: str, measurement: WeightMeasurement
    ) -> WeightMeasurement:
        recorded = self.router.record_measurement_for_user(user_id, measurement)
        self.persist_router_state()
        return recorded

    def assign_pending_measurement(
        self, measurement_id: str, user_id: str
    ) -> WeightMeasurement:
        pending = self._pending_measurements.pop(measurement_id, None)
        if pending is None:
            raise MeasurementNotFoundError("Pending measurement not found")

        try:
            recorded = self.record_measurement_for_user(user_id, pending.measurement)
        except Exception:
            self._pending_measurements[measurement_id] = pending
            raise
        if pending.notified_mobile_services and hasattr(self.hass, "async_create_task"):
            self.hass.async_create_task(
                self._clear_mobile_notifications(
                    measurement_id,
                    pending.notified_mobile_services,
                )
            )
        self._dismiss_pending_notification(measurement_id)
        self._notify()
        self._notify_diagnostic_sensors()
        return recorded

    def ignore_candidate_for_pending_measurement(
        self,
        measurement_id: str,
        user_id: str,
    ) -> bool:
        pending = self._pending_measurements.get(measurement_id)
        if pending is None:
            return False

        original_candidates = [
            candidate
            for candidate in pending.candidate_details
            if candidate["user_id"] != user_id
        ]
        if len(original_candidates) == len(pending.candidate_details):
            return False

        if not original_candidates:
            removed = self._pending_measurements.pop(measurement_id, None)
            if removed is None:
                return False
            if removed.notified_mobile_services and hasattr(
                self.hass, "async_create_task"
            ):
                self.hass.async_create_task(
                    self._clear_mobile_notifications(
                        measurement_id,
                        removed.notified_mobile_services,
                    )
                )
            self._dismiss_pending_notification(measurement_id)
            self._notify()
            self._notify_diagnostic_sensors()
            return True

        old_notified_services = list(pending.notified_mobile_services)
        pending.candidate_details = original_candidates
        if old_notified_services and hasattr(self.hass, "async_create_task"):
            self.hass.async_create_task(
                self._clear_mobile_notifications(
                    measurement_id,
                    old_notified_services,
                )
            )
        self._create_pending_notification(measurement_id)
        if hasattr(self.hass, "async_create_task"):
            self.hass.async_create_task(
                self._send_mobile_notifications_for_pending_measurement(measurement_id)
            )
        self._notify_diagnostic_sensors()
        return True

    def reassign_measurement(
        self,
        from_user_id: str,
        to_user_id: str,
        measurement_id: str | None = None,
    ) -> WeightMeasurement:
        recorded = self.router.reassign_measurement(
            from_user_id, to_user_id, measurement_id
        )
        self.persist_router_state()
        self._notify()
        self._notify_diagnostic_sensors()
        return recorded

    def remove_measurement(
        self,
        user_id: str,
        measurement_id: str | None = None,
    ) -> WeightMeasurement:
        removed = self.router.remove_measurement(user_id, measurement_id)
        self.persist_router_state()
        self._notify()
        self._notify_diagnostic_sensors()
        return removed

    @callback
    def _async_handle_source_update(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if old_state is None:
            _LOGGER.debug(
                "Skipping source update for %s: entity restored on startup (old_state is None)",
                self.source_entity_id,
            )
            return

        if not new_state or new_state.state is None:
            return
        if new_state.state in {"unavailable", "unknown", "none", "None"}:
            return

        value = _safe_float(new_state.state)
        if value is None:
            return

        unit = new_state.attributes.get("unit_of_measurement", "kg")
        weight_kg = _convert_to_kg(value, unit)
        if weight_kg is None:
            return

        old_value = (
            _safe_float(old_state.state) if hasattr(old_state, "state") else None
        )
        old_unit = (
            old_state.attributes.get("unit_of_measurement", "kg")
            if hasattr(old_state, "attributes")
            else "kg"
        )
        old_weight_kg = (
            _convert_to_kg(old_value, old_unit) if old_value is not None else None
        )
        if (
            old_weight_kg is not None
            and unit != old_unit
            and abs(old_weight_kg - weight_kg) < 0.01
        ):
            _LOGGER.debug(
                "Skipping source update for %s: unit change only, raw value unchanged",
                self.source_entity_id,
            )
            return

        measurement = WeightMeasurement(
            weight_kg=weight_kg,
            timestamp=datetime.now(tz=timezone.utc),
            source_id=self.source_entity_id,
            source_unit=unit,
            raw={
                "source_state": new_state.state,
                "source_attributed_unit": unit,
            },
        )
        candidates = self.router.evaluate_measurement(measurement)
        resolved_user_ids = self._resolve_candidate_user_ids(measurement, candidates)

        if len(resolved_user_ids) == 1:
            self.record_measurement_for_user(resolved_user_ids[0], measurement)
        elif len(resolved_user_ids) > 1:
            self._store_pending_measurement(measurement, candidates, resolved_user_ids)

        self._notify()
