"""Sensors for multi-user scale router."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant

from .const import DATA_ROUTER, DOMAIN


def _weight_to_display(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


class RouterUserWeightSensor(SensorEntity):
    """Sensor that exposes the latest assigned weight for a user."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_should_poll = False
    _attr_has_entity_name = True

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
                "Timestamp": measurement.timestamp.isoformat(),
            }
            if is_pounds:
                display_measurement["Weight (lbs)"] = round(
                    self._runtime.display_weight_value(measurement.weight_kg), 2
                )
            else:
                display_measurement["Weight (kg)"] = round(measurement.weight_kg, 2)
            weight_history.append(display_measurement)

        return {"weight_history": weight_history}

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_listener(self.async_write_ha_state)


class RouterPendingSensor(SensorEntity):
    """Diagnostic pending-measurement count."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Pending Measurements"

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
                "measurement_id": pending.measurement.measurement_id,
                "Timestamp": pending.measurement.timestamp.isoformat(),
                **(
                    {
                        "Weight (lbs)": round(
                            self._runtime.display_weight_value(
                                pending.measurement.weight_kg
                            ),
                            2,
                        )
                    }
                    if self._runtime.display_unit == "lb"
                    else {
                        "Weight (kg)": round(pending.measurement.weight_kg, 2),
                    }
                ),
            }
            for pending in self._runtime.pending_measurements
        ]
        pending.sort(key=lambda pending_item: pending_item["Timestamp"], reverse=True)
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DATA_ROUTER].get(entry.entry_id)
    if not runtime:
        return

    entities = [RouterPendingSensor(runtime), RouterUsersSensor(runtime)]
    for user in runtime.users:
        entities.append(
            RouterUserWeightSensor(runtime, user.user_id, user.display_name)
        )

    async_add_entities(entities)
