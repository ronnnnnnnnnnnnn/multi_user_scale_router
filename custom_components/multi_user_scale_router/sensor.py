"""Sensors for multi-user scale router."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant, callback

from .const import DATA_ROUTER, DOMAIN


def _weight_to_display(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


class RouterUserWeightSensor(SensorEntity):
    """Sensor that exposes the user's latest weight measurement."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_force_update = True

    def __init__(self, runtime: Any, user_id: str, display_name: str) -> None:
        self._runtime = runtime
        self._user_id = user_id
        self._attr_name = f"{display_name}'s Weight"
        self._attr_unique_id = f"{runtime.entry_id}_{user_id}_weight"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry_id)},
            name=runtime.title,
            manufacturer="Multi-User Scale Router",
            model="Multi-User Router",
        )
        runtime.add_listener(self.async_write_ha_state)

    @property
    def native_value(self) -> float | None:
        measurement = self._runtime.router.get_user_last_measurement(self._user_id)
        return _weight_to_display(
            self._runtime.display_weight_value(measurement.weight_kg)
            if measurement is not None
            else None
        )

    @property
    def native_unit_of_measurement(self) -> str:
        return self._runtime.display_unit

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        history = self._runtime.router.get_user_history(self._user_id)
        display_unit = self._runtime.display_unit
        is_pounds = display_unit == "lb"
        weight_history = []
        for measurement in history[-20:]:
            display_measurement = {
                "measurement_id": measurement.measurement_id,
                "timestamp": measurement.timestamp.isoformat(),
                "weight": round(
                    self._runtime.display_weight_value(measurement.weight_kg)
                    if is_pounds
                    else measurement.weight_kg,
                    2,
                ),
                "weight_unit": display_unit,
            }
            weight_history.append(display_measurement)

        attrs = {"weight_history": weight_history}

        measurement = self._runtime.router.get_user_last_measurement(self._user_id)
        if measurement is not None:
            attrs["measurement_id"] = measurement.measurement_id
            attrs["source_entity_id"] = self._runtime.source_entity_id

        return attrs

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_listener(self.async_write_ha_state)


class RouterPendingSensor(SensorEntity):
    """Diagnostic pending-measurement count."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Pending Measurements"
    _attr_icon = "mdi:clipboard-list"

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry_id}_pending_count"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry_id)},
            name=runtime.title,
            manufacturer="Multi-User Scale Router",
            model="Multi-User Router",
        )
        runtime.add_diagnostic_listener(self.async_write_ha_state)

    @property
    def native_value(self) -> int | None:
        return self._runtime.pending_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = [
            {
                "measurement_id": pending_item.measurement.measurement_id,
                "timestamp": pending_item.measurement.timestamp.isoformat(),
                "weight": round(
                    self._runtime.display_weight_value(
                        pending_item.measurement.weight_kg
                    )
                    if self._runtime.display_unit == "lb"
                    else pending_item.measurement.weight_kg,
                    2,
                ),
                "weight_unit": self._runtime.display_unit,
            }
            for pending_item in self._runtime.pending_measurements
        ]
        pending.sort(key=lambda x: x["timestamp"], reverse=True)
        return {
            "pending": pending,
        }

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_diagnostic_listener(self.async_write_ha_state)


class RouterUsersSensor(SensorEntity):
    """Diagnostic sensor that exposes configured router users."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "User Directory"
    _attr_icon = "mdi:account-multiple"

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry_id}_users"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry_id)},
            name=runtime.title,
            manufacturer="Multi-User Scale Router",
            model="Multi-User Router",
        )
        runtime.add_listener(self.async_write_ha_state)

    @property
    def native_value(self) -> int | None:
        return len(self._runtime.users)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "users": [
                {
                    "user_id": user.get("user_id"),
                    "name": user.get("display_name", ""),
                }
                for user in self._runtime.user_directory
            ]
        }

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_listener(self.async_write_ha_state)


class RouterUserTrackedEntitySensor(RestoreSensor):
    """Sensor that exposes a tracked entity for a user."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_force_update = True

    def __init__(
        self, runtime: Any, user_id: str, display_name: str, source_entity_id: str
    ) -> None:
        self._runtime = runtime
        self._user_id = user_id
        self._source_entity_id = source_entity_id

        state = runtime.hass.states.get(source_entity_id)

        name = source_entity_id.split(".")[-1].replace("_", " ").title()

        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(runtime.hass)
        registry_entry = registry.async_get(source_entity_id)
        if registry_entry:
            name = registry_entry.name or registry_entry.original_name or name
            self._attr_device_class = (
                registry_entry.device_class or registry_entry.original_device_class
            )
            self._attr_native_unit_of_measurement = registry_entry.unit_of_measurement
            self._attr_icon = registry_entry.icon or registry_entry.original_icon

        if state:
            name = state.attributes.get("friendly_name", name)
            self._attr_native_unit_of_measurement = state.attributes.get(
                "unit_of_measurement",
                getattr(self, "_attr_native_unit_of_measurement", None),
            )
            self._attr_device_class = state.attributes.get(
                "device_class", getattr(self, "_attr_device_class", None)
            )
            self._attr_state_class = state.attributes.get("state_class")
            self._attr_icon = state.attributes.get(
                "icon", getattr(self, "_attr_icon", None)
            )

        # Fallback to prevent device_class/unit mismatch warnings on startup if unit is missing
        if getattr(
            self, "_attr_device_class", None
        ) == SensorDeviceClass.WEIGHT and not getattr(
            self, "_attr_native_unit_of_measurement", None
        ):
            self._attr_native_unit_of_measurement = "kg"

        # state_class is not stored in the entity registry, so if the source entity
        # hasn't loaded yet we'd lose it. Tracked entities from a scale are always numeric
        # measurements, so we can safely default to MEASUREMENT to prevent
        # "entity no longer has a state class" repair warnings on startup.
        if not getattr(self, "_attr_state_class", None):
            self._attr_state_class = SensorStateClass.MEASUREMENT

        self._attr_name = f"{display_name}'s {name}"
        self._attr_unique_id = (
            f"{runtime.entry_id}_{user_id}_{source_entity_id.replace('.', '_')}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry_id)},
            name=runtime.title,
            manufacturer="Multi-User Scale Router",
            model="Multi-User Router",
        )
        self._last_seen_id: str | None = None
        self._value_measurement_id: str | None = None
        self._attr_native_value = None

    def _extract_value(self, measurement: Any) -> str | float | None:
        if measurement is None:
            return None
        tracked = measurement.raw.get("tracked_entities")
        if not tracked or not isinstance(tracked, dict):
            return None
        entity_data = tracked.get(self._source_entity_id)
        if not entity_data or not isinstance(entity_data, dict):
            return None
        return entity_data.get("state")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._value_measurement_id is None:
            return None
        return {
            "measurement_id": self._value_measurement_id,
            "source_entity_id": self._source_entity_id,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        measurement = self._runtime.router.get_user_last_measurement(self._user_id)
        value = self._extract_value(measurement)
        if value is not None:
            self._attr_native_value = value
            self._last_seen_id = measurement.measurement_id
            self._value_measurement_id = measurement.measurement_id
        else:
            last = await self.async_get_last_sensor_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value
            if measurement is not None:
                self._last_seen_id = measurement.measurement_id
        self._runtime.add_listener(self._handle_runtime_update)

    @callback
    def _handle_runtime_update(self) -> None:
        measurement = self._runtime.router.get_user_last_measurement(self._user_id)
        if measurement is None or measurement.measurement_id == self._last_seen_id:
            return
        self._last_seen_id = measurement.measurement_id
        value = self._extract_value(measurement)
        if value is None:
            # Field absent on this weigh-in: keep the last known value.
            return
        self._attr_native_value = value
        self._value_measurement_id = measurement.measurement_id
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_listener(self._handle_runtime_update)


class RouterUserTrackedAttributeSensor(RestoreSensor):
    """Sensor that exposes a tracked attribute for a user."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_force_update = True

    def __init__(
        self, runtime: Any, user_id: str, display_name: str, attribute_key: str
    ) -> None:
        self._runtime = runtime
        self._user_id = user_id
        self._attribute_key = attribute_key

        name = attribute_key.replace("_", " ").title()
        self._attr_name = f"{display_name}'s {name}"
        self._attr_unique_id = f"{runtime.entry_id}_{user_id}_attr_{attribute_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry_id)},
            name=runtime.title,
            manufacturer="Multi-User Scale Router",
            model="Multi-User Router",
        )

        key_lower = attribute_key.lower()

        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Determine unit and device class from key name.
        if "impedance" in key_lower or "resistance" in key_lower:
            self._attr_native_unit_of_measurement = "Ω"
        elif "mass" in key_lower or "weight" in key_lower:
            self._attr_device_class = SensorDeviceClass.WEIGHT
            source_state = runtime.hass.states.get(runtime.source_entity_id)
            if source_state:
                self._attr_native_unit_of_measurement = source_state.attributes.get(
                    "unit_of_measurement", "kg"
                )
            else:
                self._attr_native_unit_of_measurement = "kg"
        elif any(
            x in key_lower for x in ["percent", "percentage", "fat", "water", "protein"]
        ):
            self._attr_native_unit_of_measurement = "%"
        elif "metabolic" in key_lower or "bmr" in key_lower:
            self._attr_native_unit_of_measurement = "kcal"

        # Determine icon from key name, with source-entity fallback.
        if "impedance" in key_lower or "resistance" in key_lower:
            self._attr_icon = "mdi:omega"
        elif "fat_free" in key_lower:
            self._attr_icon = "mdi:run"
        elif "water" in key_lower:
            self._attr_icon = "mdi:water-percent"
        elif "fat" in key_lower:
            self._attr_icon = "mdi:human-handsdown"
        elif "muscle" in key_lower:
            self._attr_icon = "mdi:weight-lifter"
        elif "bone" in key_lower:
            self._attr_icon = "mdi:bone"
        elif "bmi" in key_lower or "body_mass_index" in key_lower:
            self._attr_icon = "mdi:human-male-height-variant"
        elif "metabolic" in key_lower or "bmr" in key_lower:
            self._attr_icon = "mdi:fire"
        elif "protein" in key_lower:
            self._attr_icon = "mdi:egg-fried"
        else:
            # Fall back to the source entity's icon for unrecognised keys.
            source_state = runtime.hass.states.get(runtime.source_entity_id)
            if source_state:
                self._attr_icon = source_state.attributes.get("icon")

        self._last_seen_id: str | None = None
        self._value_measurement_id: str | None = None
        self._attr_native_value = None

    def _extract_value(self, measurement: Any) -> str | float | None:
        if measurement is None:
            return None
        tracked = measurement.raw.get("tracked_attributes")
        if not tracked or not isinstance(tracked, dict):
            return None
        return tracked.get(self._attribute_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._value_measurement_id is None:
            return None
        return {
            "measurement_id": self._value_measurement_id,
            "source_entity_id": self._runtime.source_entity_id,
            "source_attribute": self._attribute_key,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        measurement = self._runtime.router.get_user_last_measurement(self._user_id)
        value = self._extract_value(measurement)
        if value is not None:
            self._attr_native_value = value
            self._last_seen_id = measurement.measurement_id
            self._value_measurement_id = measurement.measurement_id
        else:
            last = await self.async_get_last_sensor_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value
            if measurement is not None:
                self._last_seen_id = measurement.measurement_id
        self._runtime.add_listener(self._handle_runtime_update)

    @callback
    def _handle_runtime_update(self) -> None:
        measurement = self._runtime.router.get_user_last_measurement(self._user_id)
        if measurement is None or measurement.measurement_id == self._last_seen_id:
            return
        self._last_seen_id = measurement.measurement_id
        value = self._extract_value(measurement)
        if value is None:
            # Field absent on this weigh-in: keep the last known value.
            return
        self._attr_native_value = value
        self._value_measurement_id = measurement.measurement_id
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_listener(self._handle_runtime_update)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DATA_ROUTER].get(entry.entry_id)
    if not runtime:
        return

    entities = [RouterPendingSensor(runtime), RouterUsersSensor(runtime)]

    discovered_attributes = set(runtime.tracked_attributes)

    for user in runtime.users:
        entities.append(
            RouterUserWeightSensor(runtime, user.user_id, user.display_name)
        )
        for entity_id in runtime.tracked_entities:
            entities.append(
                RouterUserTrackedEntitySensor(
                    runtime, user.user_id, user.display_name, entity_id
                )
            )
        for attr_key in discovered_attributes:
            if "history" in attr_key.lower() or "list" in attr_key.lower():
                continue
            entities.append(
                RouterUserTrackedAttributeSensor(
                    runtime, user.user_id, user.display_name, attr_key
                )
            )

    async_add_entities(entities)
