"""Multi-User Scale Router integration."""

from __future__ import annotations

import logging
from urllib.parse import unquote

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    CONF_DEVICE_ID,
    CONF_FROM_USER_ID,
    CONF_MEASUREMENT_ID,
    CONF_TO_USER_ID,
    CONF_USER_ID,
    DATA_ROUTER,
    DATA_MOBILE_APP_LISTENER_UNSUB,
    DOMAIN,
    SERVICE_ASSIGN_MEASUREMENT,
    SERVICE_REASSIGN_MEASUREMENT,
    SERVICE_REMOVE_MEASUREMENT,
)
from .coordinator import RouterRuntime

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = RouterRuntime(hass, entry)
    runtime.async_setup()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = runtime
    hass.data[DATA_ROUTER] = hass.data[DOMAIN]

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data:
        data.async_unload()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        router_data = hass.data.get(DOMAIN, {})
        for service in (
            SERVICE_ASSIGN_MEASUREMENT,
            SERVICE_REASSIGN_MEASUREMENT,
            SERVICE_REMOVE_MEASUREMENT,
        ):
            hass.services.async_remove(DOMAIN, service)
        if unsub := router_data.pop(DATA_MOBILE_APP_LISTENER_UNSUB, None):
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
        if not decoded:
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
    if hass.services.has_service(DOMAIN, SERVICE_ASSIGN_MEASUREMENT):
        return

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

    hass.services.async_register(
        DOMAIN,
        SERVICE_ASSIGN_MEASUREMENT,
        handle_assign,
        schema=ASSIGN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REASSIGN_MEASUREMENT,
        handle_reassign,
        schema=REASSIGN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_MEASUREMENT,
        handle_remove,
        schema=REMOVE_SCHEMA,
    )
