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
from homeassistant.const import UnitOfMass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.unit_conversion import MassConverter

from .const import DATA_ROUTER, DOMAIN, MAX_EXPOSED_HISTORY_ITEMS, UNIT_SPELLING_ALIASES


def _weight_to_display(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def _normalize_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    normalized = unit.lower().strip()
    return UNIT_SPELLING_ALIASES.get(normalized, normalized)


def _convert_mass_value(value: Any, from_unit: str | None, to_unit: str | None) -> Any:
    """Convert a captured mass value between units when possible.

    Returns the value unchanged when the units are missing, equal, or not
    valid mass units, or when the value isn't numeric — mirroring the
    plain pass-through behavior for everything we can't safely convert.
    """
    norm_from = _normalize_unit(from_unit)
    norm_to = _normalize_unit(to_unit)
    if norm_from is None or norm_to is None or norm_from == norm_to:
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if (
        norm_from not in MassConverter.VALID_UNITS
        or norm_to not in MassConverter.VALID_UNITS
    ):
        return value
    # Unrounded on purpose — rounding is presentation, handled by
    # suggested_display_precision on the sensors.
    return MassConverter.convert(numeric, norm_from, norm_to)


class RouterUserWeightSensor(SensorEntity):
    """Sensor that exposes the user's latest weight measurement."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False
    _attr_has_entity_name = True

    # Never record the history attribute: it's already exposed capped (20
    # entries), but recording an array on every weigh-in just churns
    # database rows — and past the recorder's 16 KB attribute limit
    # (MAX_STATE_ATTRS_BYTES) the recorder would store {} for ALL of the
    # state's attributes.
    _unrecorded_attributes = frozenset({"weight_history"})

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

        history = self._runtime.router.get_user_history(self._user_id)
        self._last_history_ids = [
            m.measurement_id for m in history[-MAX_EXPOSED_HISTORY_ITEMS:]
        ]

        runtime.add_listener(self._handle_runtime_update)

    @callback
    def _handle_runtime_update(self) -> None:
        history = self._runtime.router.get_user_history(self._user_id)
        current_history_ids = [
            m.measurement_id for m in history[-MAX_EXPOSED_HISTORY_ITEMS:]
        ]

        if current_history_ids == self._last_history_ids:
            return

        self._last_history_ids = current_history_ids
        self.async_write_ha_state()

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
        # Convert to the unit the state itself is rendered in (registry
        # override → suggested → native), so attribute and state can never
        # disagree — a per-entity override to any mass unit is honored.
        display_unit = self.unit_of_measurement or self._runtime.display_unit
        weight_history = []
        # Oldest→newest; the pending diagnostic is deliberately the
        # opposite (newest→oldest).
        for measurement in history[-MAX_EXPOSED_HISTORY_ITEMS:]:
            display_measurement = {
                "measurement_id": measurement.measurement_id,
                "timestamp": measurement.timestamp.isoformat(),
                "weight": round(
                    MassConverter.convert(
                        measurement.weight_kg, UnitOfMass.KILOGRAMS, display_unit
                    ),
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
        self._runtime.remove_listener(self._handle_runtime_update)


class RouterPendingSensor(SensorEntity):
    """Diagnostic pending-measurement count."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Pending Measurements"
    _attr_icon = "mdi:clipboard-list"

    # A diagnostic work-queue with no history value — keep it out of the
    # recorder entirely.
    _unrecorded_attributes = frozenset({"pending"})

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
                    ),
                    2,
                ),
                "weight_unit": self._runtime.display_unit,
            }
            for pending_item in self._runtime.pending_measurements
        ]
        # Newest→oldest — deliberately opposite to `weight_history`
        # (oldest→newest): pending entries are a work-queue where the most
        # recent unattributed weigh-in is the one being acted on.
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

    # Bounded, but a pure listing — recording it just churns database rows.
    _unrecorded_attributes = frozenset({"users"})

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

        if getattr(self, "_attr_device_class", None) == SensorDeviceClass.WEIGHT:
            # Fallback to prevent device_class/unit mismatch warnings on
            # startup if unit is missing
            if not getattr(self, "_attr_native_unit_of_measurement", None):
                self._attr_native_unit_of_measurement = "kg"
            # Converted values are stored unrounded; keep the UI tidy.
            self._attr_suggested_display_precision = 2

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
        value = entity_data.get("state")
        if getattr(self, "_attr_device_class", None) == SensorDeviceClass.WEIGHT:
            # The value was captured in the tracked entity's unit *at weigh
            # time*, while this sensor's native unit was snapshotted at
            # setup — convert so a unit change on the tracked entity can't
            # mislabel data.
            captured_unit = (entity_data.get("attributes") or {}).get(
                "unit_of_measurement"
            )
            value = _convert_mass_value(
                value, captured_unit, self.native_unit_of_measurement
            )
        return value

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
                restored = last.native_value
                if (
                    getattr(self, "_attr_device_class", None)
                    == SensorDeviceClass.WEIGHT
                ):
                    # The stored value carries the native unit it was
                    # recorded under; this sensor's unit is re-snapshotted
                    # from the source on every setup, so convert across a
                    # unit change that happened while we were unloaded.
                    restored = _convert_mass_value(
                        restored,
                        last.native_unit_of_measurement,
                        self.native_unit_of_measurement,
                    )
                self._attr_native_value = restored
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
        self._attr_force_update = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._runtime.remove_listener(self._handle_runtime_update)


class RouterUserTrackedAttributeSensor(RestoreSensor):
    """Sensor that exposes a tracked attribute for a user."""

    _attr_should_poll = False
    _attr_has_entity_name = True

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
        elif "percent" in key_lower or "bmi" in key_lower or "index" in key_lower:
            # Checked BEFORE the mass/weight branch: muscle_mass_percentage
            # and body_mass_index contain "mass" and would otherwise get
            # WEIGHT device class — and be wrongly mass-unit-converted on a
            # source unit change. Indices (BMI) stay dimensionless.
            if "percent" in key_lower:
                self._attr_native_unit_of_measurement = "%"
        elif "mass" in key_lower or "weight" in key_lower:
            self._attr_device_class = SensorDeviceClass.WEIGHT
            source_state = runtime.hass.states.get(runtime.source_entity_id)
            if source_state:
                self._attr_native_unit_of_measurement = source_state.attributes.get(
                    "unit_of_measurement", "kg"
                )
            else:
                self._attr_native_unit_of_measurement = "kg"
            # Converted values are stored unrounded; keep the UI tidy.
            self._attr_suggested_display_precision = 2
        elif any(x in key_lower for x in ["fat", "water", "protein"]):
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
        value = tracked.get(self._attribute_key)
        if getattr(self, "_attr_device_class", None) == SensorDeviceClass.WEIGHT:
            # Tracked attributes ride on the weight source's state, so
            # their values arrive in the source's unit *at capture time*,
            # while this sensor's native unit was snapshotted at setup —
            # convert so a source unit change can't mislabel data.
            captured_unit = getattr(measurement, "source_unit", None) or (
                measurement.raw.get("source_attributed_unit")
            )
            value = _convert_mass_value(
                value, captured_unit, self.native_unit_of_measurement
            )
        return value

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
                restored = last.native_value
                if (
                    getattr(self, "_attr_device_class", None)
                    == SensorDeviceClass.WEIGHT
                ):
                    # The stored value carries the native unit it was
                    # recorded under; this sensor's unit is re-snapshotted
                    # from the source on every setup, so convert across a
                    # unit change that happened while we were unloaded.
                    restored = _convert_mass_value(
                        restored,
                        last.native_unit_of_measurement,
                        self.native_unit_of_measurement,
                    )
                self._attr_native_value = restored
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
        self._attr_force_update = True
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
