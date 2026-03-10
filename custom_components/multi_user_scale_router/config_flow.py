"""Config flow for the multi-user scale router integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)

from multi_user_scale_core import RouterConfig, UserProfile, WeightRouter

from .const import (
    CONF_HISTORY_RETENTION_DAYS,
    CONF_MAX_HISTORY_SIZE,
    CONF_MOBILE_NOTIFY_SERVICES,
    CONF_PERSON_ENTITY,
    CONF_ROUTER_STATE,
    CONF_SOURCE_ENTITY_ID,
    CONF_USER_ID,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_MAX_HISTORY_SIZE,
    DOMAIN,
)

CONF_USER_NAME = "user_name"
CONF_ADD_ANOTHER_USER = "add_another_user"


def _slugify_user(value: str, index: int) -> str:
    base = "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
    if not base:
        base = f"user_{index}"
    return base


def _create_user_id(display_name: str, existing_users: list[dict[str, Any]]) -> str:
    user_id = _slugify_user(display_name, len(existing_users))
    existing_ids = {
        user.get(CONF_USER_ID)
        for user in existing_users
        if isinstance(user.get(CONF_USER_ID), str)
    }
    suffix = 2
    candidate = user_id
    while candidate in existing_ids:
        candidate = f"{user_id}_{suffix}"
        suffix += 1
    return candidate


def _validate_user_name_not_empty(user_name: str) -> bool:
    return bool(user_name and user_name.strip())


def _validate_person_entity_unique(
    person_entity: str | None,
    users: list[dict[str, Any]],
    exclude_user_id: str | None = None,
) -> bool:
    if not person_entity:
        return True

    for user in users:
        if user.get(CONF_USER_ID) == exclude_user_id:
            continue
        if user.get(CONF_PERSON_ENTITY) == person_entity:
            return False
    return True


def _build_user(
    display_name: str,
    existing_users: list[dict[str, Any]],
    person_entity: str | None = None,
    mobile_notify_services: list[str] | None = None,
) -> dict[str, Any]:
    user = {
        CONF_USER_ID: _create_user_id(display_name, existing_users),
        "display_name": display_name,
    }
    if person_entity:
        user[CONF_PERSON_ENTITY] = person_entity
    if mobile_notify_services:
        user[CONF_MOBILE_NOTIFY_SERVICES] = mobile_notify_services
    return user


def _normalize_mobile_services(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _get_mobile_notify_services(hass) -> dict[str, str]:
    services: dict[str, str] = {}
    hass_services = getattr(hass, "services", None)
    if hass_services is None or not hasattr(hass_services, "async_services"):
        return services

    notify_services = hass_services.async_services().get("notify", {})
    for service_name in notify_services.keys():
        if service_name.startswith("mobile_app_"):
            display_name = (
                service_name.replace("mobile_app_", "").replace("_", " ").title()
            )
            services[service_name] = display_name
    return services


def _source_sensor_options(hass) -> list[selector.SelectOptionDict]:
    registry = er.async_get(hass)
    options: list[tuple[int, selector.SelectOptionDict]] = []
    for state in hass.states.async_all("sensor"):
        if not _is_supported_weight_sensor(state):
            continue
        if not _is_numeric_state(state):
            continue
        registry_entry = registry.async_get(state.entity_id)
        if registry_entry and registry_entry.platform == DOMAIN:
            continue
        label = state.attributes.get("friendly_name") or state.entity_id
        option = selector.SelectOptionDict(value=state.entity_id, label=str(label))
        options.append(
            (_source_sensor_relevance_score(state), option)
        )
    options.sort(
        key=lambda option_with_score: (
            -option_with_score[0],
            option_with_score[1]["label"].lower(),
        )
    )
    return [option for _score, option in options]


def _is_numeric_state(state) -> bool:
    if state is None:
        return False

    if state.state not in {"unknown", "unavailable", "None", "none"}:
        try:
            float(state.state)
        except (TypeError, ValueError):
            return False
        return True

    return _has_numeric_metadata(state)


def _has_numeric_metadata(state) -> bool:
    device_class = state.attributes.get(ATTR_DEVICE_CLASS)
    if device_class == SensorDeviceClass.WEIGHT:
        return True

    unit = str(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, "")).lower().strip()
    state_class = str(state.attributes.get("state_class", "")).lower().strip()
    return bool(unit) and unit in {
        "kg",
        "kilogram",
        "kilograms",
        "lb",
        "lbs",
        "pound",
        "pounds",
    } and state_class == "measurement"


def _is_supported_weight_sensor(state) -> bool:
    device_class = state.attributes.get(ATTR_DEVICE_CLASS)
    if device_class == SensorDeviceClass.WEIGHT:
        return True

    unit = str(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, "")).lower().strip()
    return unit in {"kg", "kilogram", "kilograms", "lb", "lbs", "pound", "pounds"}


def _source_sensor_relevance_score(state) -> int:
    device_class = state.attributes.get(ATTR_DEVICE_CLASS)
    name = str(state.attributes.get("friendly_name", state.entity_id)).lower()
    score = 0

    if device_class == SensorDeviceClass.WEIGHT:
        score += 120

    if "weight" in name:
        score += 50
    if "scale" in name:
        score += 20
    if "mass" in name:
        score -= 10    
    if any(term in name for term in {"fat", "muscle", "bone", "water", "impedance"}):
        score -= 25

    return score


def _sync_router_state(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    router_state = payload.get(CONF_ROUTER_STATE)
    if not isinstance(router_state, dict):
        return payload

    try:
        router = WeightRouter.from_dict(router_state)
        router.set_config(
            RouterConfig(
                history_retention_days=payload.get(
                    CONF_HISTORY_RETENTION_DAYS,
                    DEFAULT_HISTORY_RETENTION_DAYS,
                ),
                max_history_size=payload.get(
                    CONF_MAX_HISTORY_SIZE,
                    DEFAULT_MAX_HISTORY_SIZE,
                ),
            )
        )
        router.set_users(
            [UserProfile.from_dict(user) for user in payload.get("users", [])]
        )
        payload[CONF_ROUTER_STATE] = router.to_dict()
    except (TypeError, ValueError, KeyError):
        payload.pop(CONF_ROUTER_STATE, None)
    return payload


def _source_entity_title(hass, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is None:
        return entity_id
    return str(state.attributes.get("friendly_name") or entity_id)


class ScaleRouterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for router setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.context.update(
                {
                    CONF_SOURCE_ENTITY_ID: user_input[CONF_SOURCE_ENTITY_ID],
                    CONF_HISTORY_RETENTION_DAYS: user_input[
                        CONF_HISTORY_RETENTION_DAYS
                    ],
                    CONF_MAX_HISTORY_SIZE: user_input[CONF_MAX_HISTORY_SIZE],
                    "users": [],
                }
            )
            return await self.async_step_add_first_user()

        if not _source_sensor_options(self.hass):
            return self.async_show_form(
                step_id="user",
                errors={"base": "no_sensor_entities"},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
        )

    async def async_step_add_first_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        configured_users = list(self.context.get("users", []))
        if user_input is not None:
            user_name = user_input[CONF_USER_NAME]
            person_entity = user_input.get(CONF_PERSON_ENTITY)
            mobile_notify_services = _normalize_mobile_services(
                user_input.get(CONF_MOBILE_NOTIFY_SERVICES, [])
            )
            errors: dict[str, str] = {}

            if not _validate_user_name_not_empty(user_name):
                errors["base"] = "empty_user_name"
            if person_entity and not _validate_person_entity_unique(
                person_entity, configured_users
            ):
                errors[CONF_PERSON_ENTITY] = "duplicate_person_entity"

            if errors:
                return self.async_show_form(
                    step_id="add_first_user",
                    data_schema=self._add_first_user_schema(user_input),
                    errors=errors,
                )

            updated_users = list(configured_users)
            updated_users.append(
                _build_user(
                    user_name.strip(),
                    updated_users,
                    person_entity,
                    mobile_notify_services,
                )
            )
            self.context["users"] = updated_users

            if user_input[CONF_ADD_ANOTHER_USER]:
                return await self.async_step_add_first_user()

            data = {
                "users": updated_users,
                CONF_SOURCE_ENTITY_ID: self.context[CONF_SOURCE_ENTITY_ID],
                CONF_HISTORY_RETENTION_DAYS: self.context[CONF_HISTORY_RETENTION_DAYS],
                CONF_MAX_HISTORY_SIZE: self.context[CONF_MAX_HISTORY_SIZE],
            }
            return self.async_create_entry(
                title=(
                    f"Multi-User Scale Router ({_source_entity_title(self.hass, self.context[CONF_SOURCE_ENTITY_ID])})"
                ),
                data=data,
            )

        return self.async_show_form(
            step_id="add_first_user",
            data_schema=self._add_first_user_schema(),
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ScaleRouterOptionsFlow(config_entry)

    def _user_schema(self, user_input: dict[str, Any] | None = None) -> vol.Schema:
        sensor_options = _source_sensor_options(self.hass)
        defaults = user_input or {}
        default_source = defaults.get(
            CONF_SOURCE_ENTITY_ID,
            sensor_options[0]["value"] if sensor_options else None,
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_SOURCE_ENTITY_ID,
                    default=default_source,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=sensor_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_HISTORY_RETENTION_DAYS,
                    default=defaults.get(
                        CONF_HISTORY_RETENTION_DAYS,
                        DEFAULT_HISTORY_RETENTION_DAYS,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(
                    CONF_MAX_HISTORY_SIZE,
                    default=defaults.get(
                        CONF_MAX_HISTORY_SIZE, DEFAULT_MAX_HISTORY_SIZE
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
            }
        )

    def _add_first_user_schema(
        self, user_input: dict[str, Any] | None = None
    ) -> vol.Schema:
        defaults = user_input or {}
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_USER_NAME,
                default=defaults.get(CONF_USER_NAME, "User 1"),
            ): cv.string,
        }
        if defaults.get(CONF_PERSON_ENTITY):
            schema[
                vol.Optional(
                    CONF_PERSON_ENTITY,
                    default=defaults.get(CONF_PERSON_ENTITY),
                )
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="person"))
        else:
            schema[vol.Optional(CONF_PERSON_ENTITY)] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="person")
            )
        available_mobile_services = _get_mobile_notify_services(self.hass)
        if available_mobile_services:
            schema[
                vol.Optional(
                    CONF_MOBILE_NOTIFY_SERVICES,
                    default=defaults.get(CONF_MOBILE_NOTIFY_SERVICES, []),
                )
            ] = cv.multi_select(available_mobile_services)
        schema[
            vol.Required(
                CONF_ADD_ANOTHER_USER,
                default=defaults.get(CONF_ADD_ANOTHER_USER, False),
            )
        ] = cv.boolean
        return vol.Schema(schema)


class ScaleRouterOptionsFlow(OptionsFlow):
    """Handle router options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._users = list(config_entry.data.get("users", []))

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        menu_options = ["add_user", "router_settings"]
        if self._users:
            menu_options.insert(1, "edit_user")
            if len(self._users) > 1:
                menu_options.insert(2, "remove_user")

        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_add_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            user_name = user_input[CONF_USER_NAME]
            person_entity = user_input.get(CONF_PERSON_ENTITY)
            mobile_notify_services = _normalize_mobile_services(
                user_input.get(CONF_MOBILE_NOTIFY_SERVICES, [])
            )
            errors: dict[str, str] = {}

            if not _validate_user_name_not_empty(user_name):
                errors["base"] = "empty_user_name"
            if not _validate_person_entity_unique(person_entity, self._users):
                errors[CONF_PERSON_ENTITY] = "duplicate_person_entity"

            if errors:
                return self.async_show_form(
                    step_id="add_user",
                    data_schema=self._user_details_schema(user_input),
                    errors=errors,
                )

            updated_users = list(self._users)
            updated_users.append(
                _build_user(
                    user_name.strip(),
                    updated_users,
                    person_entity,
                    mobile_notify_services,
                )
            )
            return await self._update_entry(users=updated_users)

        return self.async_show_form(
            step_id="add_user",
            data_schema=self._user_details_schema(),
        )

    async def async_step_edit_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.context["selected_user_id"] = user_input[CONF_USER_ID]
            return await self.async_step_edit_user_details()

        user_options = {
            user[CONF_USER_ID]: user["display_name"]
            for user in self._users
            if isinstance(user.get(CONF_USER_ID), str)
        }
        return self.async_show_form(
            step_id="edit_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USER_ID): vol.In(user_options),
                }
            ),
        )

    async def async_step_edit_user_details(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        selected_user_id = self.context["selected_user_id"]
        current_user = next(
            user for user in self._users if user.get(CONF_USER_ID) == selected_user_id
        )

        if user_input is not None:
            user_name = user_input[CONF_USER_NAME]
            person_entity = user_input.get(CONF_PERSON_ENTITY)
            if CONF_MOBILE_NOTIFY_SERVICES in user_input:
                mobile_notify_services = _normalize_mobile_services(
                    user_input.get(CONF_MOBILE_NOTIFY_SERVICES, [])
                )
            else:
                mobile_notify_services = _normalize_mobile_services(
                    current_user.get(CONF_MOBILE_NOTIFY_SERVICES, [])
                )
            errors: dict[str, str] = {}

            if not _validate_user_name_not_empty(user_name):
                errors["base"] = "empty_user_name"
            if not _validate_person_entity_unique(
                person_entity,
                self._users,
                exclude_user_id=selected_user_id,
            ):
                errors[CONF_PERSON_ENTITY] = "duplicate_person_entity"

            if errors:
                return self.async_show_form(
                    step_id="edit_user_details",
                    data_schema=self._user_details_schema(user_input, current_user),
                    errors=errors,
                )

            updated_users = []
            for user in self._users:
                if user.get(CONF_USER_ID) != selected_user_id:
                    updated_users.append(user)
                    continue

                updated_user = {
                    CONF_USER_ID: selected_user_id,
                    "display_name": user_name.strip(),
                }
                if person_entity:
                    updated_user[CONF_PERSON_ENTITY] = person_entity
                if mobile_notify_services or (
                    CONF_MOBILE_NOTIFY_SERVICES in current_user
                    and CONF_MOBILE_NOTIFY_SERVICES not in user_input
                ):
                    updated_user[CONF_MOBILE_NOTIFY_SERVICES] = mobile_notify_services
                updated_users.append(updated_user)

            return await self._update_entry(users=updated_users)

        return self.async_show_form(
            step_id="edit_user_details",
            data_schema=self._user_details_schema(current_user=current_user),
        )

    async def async_step_remove_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if len(self._users) <= 1:
            return self.async_abort(reason="cannot_remove_last_user")

        if user_input is not None:
            selected_user_id = user_input[CONF_USER_ID]
            updated_users = [
                user
                for user in self._users
                if user.get(CONF_USER_ID) != selected_user_id
            ]
            return await self._update_entry(users=updated_users)

        user_options = {
            user[CONF_USER_ID]: user["display_name"]
            for user in self._users
            if isinstance(user.get(CONF_USER_ID), str)
        }
        return self.async_show_form(
            step_id="remove_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USER_ID): vol.In(user_options),
                }
            ),
        )

    async def async_step_router_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return await self._update_entry(
                **{
                    CONF_SOURCE_ENTITY_ID: user_input[CONF_SOURCE_ENTITY_ID],
                    CONF_HISTORY_RETENTION_DAYS: user_input[
                        CONF_HISTORY_RETENTION_DAYS
                    ],
                    CONF_MAX_HISTORY_SIZE: user_input[CONF_MAX_HISTORY_SIZE],
                }
            )

        return self.async_show_form(
            step_id="router_settings",
            data_schema=self._router_settings_schema(),
        )

    def _user_details_schema(
        self,
        user_input: dict[str, Any] | None = None,
        current_user: dict[str, Any] | None = None,
    ) -> vol.Schema:
        defaults = user_input or current_user or {}
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_USER_NAME,
                default=defaults.get(CONF_USER_NAME, defaults.get("display_name", "")),
            ): cv.string,
        }

        current_person_entity = defaults.get(CONF_PERSON_ENTITY)
        if current_person_entity:
            schema[
                vol.Optional(
                    CONF_PERSON_ENTITY,
                    default=current_person_entity,
                )
            ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="person"))
        else:
            schema[vol.Optional(CONF_PERSON_ENTITY)] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="person")
            )
        available_mobile_services = _get_mobile_notify_services(self.hass)
        if available_mobile_services:
            schema[
                vol.Optional(
                    CONF_MOBILE_NOTIFY_SERVICES,
                    default=defaults.get(CONF_MOBILE_NOTIFY_SERVICES, []),
                )
            ] = cv.multi_select(available_mobile_services)
        return vol.Schema(schema)

    def _router_settings_schema(
        self, user_input: dict[str, Any] | None = None
    ) -> vol.Schema:
        defaults = user_input or self._config_entry.data
        sensor_options = _source_sensor_options(self.hass)
        default_source = defaults.get(
            CONF_SOURCE_ENTITY_ID,
            sensor_options[0]["value"] if sensor_options else None,
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_SOURCE_ENTITY_ID,
                    default=default_source,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=sensor_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_HISTORY_RETENTION_DAYS,
                    default=defaults.get(
                        CONF_HISTORY_RETENTION_DAYS,
                        DEFAULT_HISTORY_RETENTION_DAYS,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Required(
                    CONF_MAX_HISTORY_SIZE,
                    default=defaults.get(
                        CONF_MAX_HISTORY_SIZE, DEFAULT_MAX_HISTORY_SIZE
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
            }
        )

    async def _update_entry(self, **changes: Any) -> FlowResult:
        updated_data = {**self._config_entry.data, **changes}
        updated_data = _sync_router_state(updated_data)
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=updated_data
        )
        await self.hass.config_entries.async_reload(self._config_entry.entry_id)
        self._users = list(updated_data.get("users", []))
        return self.async_create_entry(title="", data={})
