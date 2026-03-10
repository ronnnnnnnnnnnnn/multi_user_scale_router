"""Constants for the multi-user scale router integration."""

from __future__ import annotations

DOMAIN = "multi_user_scale_router"

CONF_SOURCE_ENTITY_ID = "source_entity_id"
CONF_PERSON_ENTITY = "person_entity"
CONF_MOBILE_NOTIFY_SERVICES = "mobile_notify_services"
CONF_HISTORY_RETENTION_DAYS = "history_retention_days"
CONF_MAX_HISTORY_SIZE = "max_history_size"
CONF_MIN_TOLERANCE_KG = "min_tolerance_kg"

SERVICE_ASSIGN_MEASUREMENT = "assign_measurement"
SERVICE_REASSIGN_MEASUREMENT = "reassign_measurement"
SERVICE_REMOVE_MEASUREMENT = "remove_measurement"
CONF_DEVICE_ID = "device_id"
CONF_MEASUREMENT_ID = "measurement_id"
CONF_USER_ID = "user_id"
CONF_FROM_USER_ID = "from_user_id"
CONF_TO_USER_ID = "to_user_id"
CONF_ROUTER_STATE = "router_state"

DATA_ROUTER = "multi_user_scale_router_data"
DATA_MOBILE_APP_LISTENER_UNSUB = "multi_user_scale_router_mobile_app_listener_unsub"

ATTR_UNIT = "unit_of_measurement"

DEFAULT_HISTORY_RETENTION_DAYS = 90
DEFAULT_MAX_HISTORY_SIZE = 100
DEFAULT_MIN_TOLERANCE_KG = 1.5
MAX_PENDING_MEASUREMENTS = 10
