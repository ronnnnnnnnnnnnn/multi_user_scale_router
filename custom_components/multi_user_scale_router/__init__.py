"""Multi-User Scale Router integration."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from urllib.parse import unquote

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_ID,
    CONF_FROM_USER_ID,
    CONF_MEASUREMENTS,
    CONF_MEASUREMENT_ID,
    CONF_TO_USER_ID,
    CONF_USER_ID,
    DATA_ROUTER,
    DATA_MOBILE_APP_LISTENER_UNSUB,
    DOMAIN,
    SERVICE_ASSIGN_MEASUREMENT,
    SERVICE_IMPORT_HISTORY,
    SERVICE_REASSIGN_MEASUREMENT,
    SERVICE_REMOVE_MEASUREMENT,
)
from .coordinator import RouterRuntime
from .repairs import (
    async_clear_repair_issues_for_entry,
    async_scan_repair_issues,
)
from multi_user_scale_core import DuplicateMeasurementError, WeightMeasurement

PLATFORMS = ["sensor"]
_LOGGER = logging.getLogger(__name__)
_MOBILE_ASSIGN_ACTION_PREFIX = "ROUTER_ASSIGN_"
_MOBILE_NOT_ME_ACTION_PREFIX = "ROUTER_NOT_ME_"
_MOBILE_ACTION_DELIMITER = "|"

ASSIGN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_MEASUREMENT_ID): cv.string,
        vol.Required(CONF_USER_ID): cv.string,
    }
)

REASSIGN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_FROM_USER_ID): cv.string,
        vol.Required(CONF_TO_USER_ID): cv.string,
        vol.Optional(CONF_MEASUREMENT_ID): cv.string,
    }
)

REMOVE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_USER_ID): cv.string,
        vol.Optional(CONF_MEASUREMENT_ID): cv.string,
    }
)

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_USER_ID): cv.string,
        vol.Required(CONF_MEASUREMENTS): vol.All(
            [
                {
                    vol.Required("weight_kg"): vol.Coerce(float),
                    vol.Required("timestamp"): cv.string,
                    vol.Optional(CONF_MEASUREMENT_ID): cv.string,
                    vol.Optional("source_id", default="history_import"): cv.string,
                    vol.Optional("source_unit", default="kg"): cv.string,
                }
            ],
            vol.Length(min=1),
        ),
    }
)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry to a newer schema version."""
    _LOGGER.debug("Migrating config entry from version %s", config_entry.version)

    if config_entry.version == 1:
        # Current version — nothing to migrate.
        return True

    # Future migrations go here, e.g.:
    #   if config_entry.version == 1:
    #       new_data = {**config_entry.data, "new_field": default_value}
    #       hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)

    _LOGGER.error(
        "Cannot migrate config entry from unknown version %s", config_entry.version
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = RouterRuntime(hass, entry)
    runtime.async_setup()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = runtime
    hass.data[DATA_ROUTER] = hass.data[DOMAIN]

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass)

    # Reconcile Settings → Repairs issues for this entry's profiles.
    # Runs again on options-flow reload because that re-enters setup.
    async_scan_repair_issues(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async_clear_repair_issues_for_entry(hass, entry)

    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data:
        data.async_unload()
        hass.data[DOMAIN].pop(entry.entry_id, None)

    remaining_runtimes = [
        v for v in hass.data.get(DOMAIN, {}).values() if isinstance(v, RouterRuntime)
    ]
    if not remaining_runtimes:
        domain_data = hass.data.get(DOMAIN, {})
        for service in (
            SERVICE_ASSIGN_MEASUREMENT,
            SERVICE_REASSIGN_MEASUREMENT,
            SERVICE_REMOVE_MEASUREMENT,
            SERVICE_IMPORT_HISTORY,
        ):
            hass.services.async_remove(DOMAIN, service)
        if unsub := domain_data.pop(DATA_MOBILE_APP_LISTENER_UNSUB, None):
            unsub()

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True


def _format_user_choices(runtime: RouterRuntime) -> str:
    users = [f"{user.display_name} ({user.user_id})" for user in runtime.users]
    return ", ".join(users) if users else "No configured users"


def _format_pending_ids(runtime: RouterRuntime) -> str:
    measurement_ids = [
        item["measurement_id"] for item in runtime.pending_measurement_details
    ]
    return ", ".join(measurement_ids) if measurement_ids else "No pending measurements"


def _format_user_history_ids(runtime: RouterRuntime, user_id: str) -> str:
    history = runtime.router.get_user_history(user_id)
    measurement_ids = [measurement.measurement_id for measurement in reversed(history)]
    return ", ".join(measurement_ids) if measurement_ids else "No measurements"


def _get_runtime_for_call(hass: HomeAssistant, call: ServiceCall) -> RouterRuntime:
    device_id = call.data[CONF_DEVICE_ID]
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(device_id)
    if device_entry is None:
        raise HomeAssistantError(f"Could not find device '{device_id}'")

    for entry_id in device_entry.config_entries:
        runtime = hass.data.get(DATA_ROUTER, {}).get(entry_id)
        if runtime is not None:
            return runtime

    raise HomeAssistantError(
        f"Device '{device_entry.name or device_id}' is not a multi-user scale router"
    )


def _decode_router_assign_action(action: str) -> tuple[str, str, str] | None:
    return _decode_router_action(action, _MOBILE_ASSIGN_ACTION_PREFIX)


def _decode_router_not_me_action(action: str) -> tuple[str, str, str] | None:
    return _decode_router_action(action, _MOBILE_NOT_ME_ACTION_PREFIX)


def _decode_router_action(action: str, prefix: str) -> tuple[str, str, str] | None:
    if not action.startswith(prefix):
        return None

    payload = action[len(prefix) :]
    if _MOBILE_ACTION_DELIMITER in payload:
        if payload.count(_MOBILE_ACTION_DELIMITER) != 2:
            return None
        try:
            entry_id, measurement_id, user_id = (
                unquote(part) for part in payload.split(_MOBILE_ACTION_DELIMITER)
            )
            return entry_id, measurement_id, user_id
        except Exception:
            return None

    parts = payload.rsplit("_", 2)
    if len(parts) != 3:
        return None

    try:
        entry_id, measurement_id, user_id = (unquote(part) for part in parts)
    except Exception:
        return None

    if not entry_id or not measurement_id:
        return None

    return entry_id, measurement_id, user_id


def _register_mobile_action_listener(hass: HomeAssistant) -> None:
    runtime_data = hass.data.setdefault(DOMAIN, {})
    if runtime_data.get(DATA_MOBILE_APP_LISTENER_UNSUB) is not None:
        return
    if not hasattr(hass, "bus") or not hasattr(hass.bus, "async_listen"):
        _LOGGER.debug(
            "Home Assistant bus is unavailable; skipping mobile action listener registration"
        )
        return

    async def handle_mobile_app_notification_action(event: object) -> None:
        event_data = getattr(event, "data", None)
        if not isinstance(event_data, dict):
            return

        action = event_data.get("action")
        if not isinstance(action, str):
            return

        decoded = _decode_router_assign_action(action)
        is_assign = decoded is not None
        if decoded is None:
            decoded = _decode_router_not_me_action(action)
            if decoded is None:
                return

        entry_id, measurement_id, user_id = decoded
        runtime = hass.data.get(DATA_ROUTER, {}).get(entry_id)
        if runtime is None:
            _LOGGER.debug(
                "Ignoring mobile action for unknown router entry '%s'", entry_id
            )
            return

        valid_user_ids = {user.user_id for user in runtime.users}
        if user_id not in valid_user_ids:
            _LOGGER.warning(
                "Ignoring mobile action with unknown user_id '%s' for entry '%s'",
                user_id,
                entry_id,
            )
            return

        if measurement_id not in {
            item["measurement_id"] for item in runtime.pending_measurement_details
        }:
            _LOGGER.debug(
                "Ignoring stale mobile action for entry '%s' and measurement '%s'",
                entry_id,
                measurement_id,
            )
            return

        if not is_assign:
            try:
                runtime.ignore_candidate_for_pending_measurement(
                    measurement_id, user_id
                )
            except Exception as error:
                _LOGGER.warning(
                    "Failed to ignore pending measurement %s for user '%s': %s",
                    measurement_id,
                    user_id,
                    error,
                )
            _LOGGER.debug(
                "User %s indicated measurement %s is not theirs (mobile action)",
                user_id,
                measurement_id,
            )
            return

        try:
            runtime.assign_pending_measurement(measurement_id, user_id)
            _LOGGER.info(
                "Assigned measurement %s to user %s from mobile action",
                measurement_id,
                user_id,
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to assign measurement %s from mobile action for user '%s': %s",
                measurement_id,
                user_id,
                error,
            )

    runtime_data[DATA_MOBILE_APP_LISTENER_UNSUB] = hass.bus.async_listen(
        "mobile_app_notification_action", handle_mobile_app_notification_action
    )
    _LOGGER.debug(
        "Registered mobile action listener for router notification assignments"
    )


def _register_services(hass: HomeAssistant) -> None:
    _register_mobile_action_listener(hass)

    async def handle_assign(call: ServiceCall) -> None:
        runtime = _get_runtime_for_call(hass, call)
        measurement_id = call.data[CONF_MEASUREMENT_ID]
        user_id = call.data[CONF_USER_ID]
        valid_user_ids = {user.user_id for user in runtime.users}
        if user_id not in valid_user_ids:
            raise HomeAssistantError(
                f"Unknown user_id '{user_id}'. Valid users: {_format_user_choices(runtime)}"
            )
        if measurement_id not in {
            item["measurement_id"] for item in runtime.pending_measurement_details
        }:
            raise HomeAssistantError(
                "Unknown pending measurement_id "
                f"'{measurement_id}'. Pending measurements: {_format_pending_ids(runtime)}"
            )
        try:
            runtime.assign_pending_measurement(measurement_id, user_id)
        except Exception as error:
            raise HomeAssistantError(str(error)) from error

    async def handle_reassign(call: ServiceCall) -> None:
        runtime = _get_runtime_for_call(hass, call)
        from_user_id = call.data[CONF_FROM_USER_ID]
        to_user_id = call.data[CONF_TO_USER_ID]
        measurement_id = call.data.get(CONF_MEASUREMENT_ID)
        valid_user_ids = {user.user_id for user in runtime.users}
        if from_user_id not in valid_user_ids:
            raise HomeAssistantError(
                "Unknown from_user_id "
                f"'{from_user_id}'. Valid users: {_format_user_choices(runtime)}"
            )
        if to_user_id not in valid_user_ids:
            raise HomeAssistantError(
                f"Unknown to_user_id '{to_user_id}'. Valid users: {_format_user_choices(runtime)}"
            )
        if measurement_id is not None and measurement_id not in {
            measurement.measurement_id
            for measurement in runtime.router.get_user_history(from_user_id)
        }:
            raise HomeAssistantError(
                "Unknown measurement_id "
                f"'{measurement_id}' for user '{from_user_id}'. "
                f"Available measurements: {_format_user_history_ids(runtime, from_user_id)}"
            )
        try:
            runtime.reassign_measurement(from_user_id, to_user_id, measurement_id)
        except Exception as error:
            raise HomeAssistantError(str(error)) from error

    async def handle_remove(call: ServiceCall) -> None:
        runtime = _get_runtime_for_call(hass, call)
        user_id = call.data[CONF_USER_ID]
        measurement_id = call.data.get(CONF_MEASUREMENT_ID)
        valid_user_ids = {user.user_id for user in runtime.users}
        if user_id not in valid_user_ids:
            raise HomeAssistantError(
                f"Unknown user_id '{user_id}'. Valid users: {_format_user_choices(runtime)}"
            )
        if measurement_id is not None and measurement_id not in {
            measurement.measurement_id
            for measurement in runtime.router.get_user_history(user_id)
        }:
            raise HomeAssistantError(
                "Unknown measurement_id "
                f"'{measurement_id}' for user '{user_id}'. "
                f"Available measurements: {_format_user_history_ids(runtime, user_id)}"
            )
        try:
            runtime.remove_measurement(user_id, measurement_id)
        except Exception as error:
            raise HomeAssistantError(str(error)) from error

    async def handle_import_history(call: ServiceCall) -> None:
        runtime = _get_runtime_for_call(hass, call)
        user_id = call.data[CONF_USER_ID]
        valid_user_ids = {user.user_id for user in runtime.users}
        if user_id not in valid_user_ids:
            raise HomeAssistantError(
                f"Unknown user_id '{user_id}'. Valid users: {_format_user_choices(runtime)}"
            )

        imported = 0
        skipped_duplicates = 0
        measurements: list[WeightMeasurement] = []
        for item in call.data[CONF_MEASUREMENTS]:
            weight_kg = item["weight_kg"]
            if not math.isfinite(weight_kg):
                raise HomeAssistantError(
                    "Imported measurement weight_kg must be a finite number"
                )

            timestamp = dt_util.parse_datetime(item["timestamp"])
            if timestamp is None:
                raise HomeAssistantError(
                    f"Invalid timestamp '{item['timestamp']}' for imported measurement"
                )
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise HomeAssistantError(
                    "Imported measurement timestamp must include a timezone offset"
                )
            timestamp = dt_util.as_utc(timestamp)
            if timestamp > datetime.now(timezone.utc):
                raise HomeAssistantError(
                    "Imported measurement timestamp must not be in the future"
                )

            measurement_id = item.get(CONF_MEASUREMENT_ID)
            if measurement_id is None:
                measurement_id = (
                    f"import_{user_id}_{item['timestamp']}_{item['weight_kg']}".replace(
                        ":", ""
                    ).replace("+", "p")
                )
            measurements.append(
                WeightMeasurement(
                    weight_kg=weight_kg,
                    timestamp=timestamp,
                    source_id=item.get("source_id", "history_import"),
                    measurement_id=measurement_id,
                    source_unit=item.get("source_unit", "kg"),
                    raw={"imported_by": SERVICE_IMPORT_HISTORY},
                )
            )

        measurements.sort(key=lambda measurement: measurement.timestamp)
        for measurement in measurements:
            try:
                runtime.record_measurement_for_user(user_id, measurement)
                imported += 1
            except DuplicateMeasurementError:
                skipped_duplicates += 1
                continue
            except Exception as error:
                raise HomeAssistantError(str(error)) from error

        runtime._notify()
        runtime._notify_diagnostic_sensors()
        _LOGGER.info(
            "Imported %s historical measurements for %s, skipped %s duplicates",
            imported,
            user_id,
            skipped_duplicates,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_ASSIGN_MEASUREMENT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ASSIGN_MEASUREMENT,
            handle_assign,
            schema=ASSIGN_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_REASSIGN_MEASUREMENT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REASSIGN_MEASUREMENT,
            handle_reassign,
            schema=REASSIGN_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_MEASUREMENT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_MEASUREMENT,
            handle_remove,
            schema=REMOVE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            handle_import_history,
            schema=IMPORT_HISTORY_SCHEMA,
        )
