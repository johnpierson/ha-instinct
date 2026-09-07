"""Constants for the Instinct integration."""

from __future__ import annotations

DOMAIN = "instinct"

# Config entry keys
CONF_NOTIFY_SERVICE = "notify_service"
CONF_WEBHOOK_ID = "webhook_id"

# Options keys
CONF_HISTORY_DAYS = "history_days"
CONF_TIME_WINDOW_MINUTES = "time_window_minutes"
CONF_MIN_SCORE = "min_score"
CONF_AUTO_CONFIDENCE = "auto_execute_confidence"
CONF_AUTO_MIN_SAMPLES = "auto_execute_min_samples"
CONF_DOMAINS = "domains"
CONF_EXCLUDE_ENTITIES = "exclude_entities"
CONF_MULTI_ACTION = "multi_action"
CONF_MAX_ACTIONS = "max_actions"
CONF_MANUAL_ONLY = "manual_only"

# Defaults
DEFAULT_HISTORY_DAYS = 30
DEFAULT_TIME_WINDOW_MINUTES = 60
DEFAULT_MIN_SCORE = 1.0
DEFAULT_AUTO_CONFIDENCE = 0.9
DEFAULT_AUTO_MIN_SAMPLES = 8
DEFAULT_DOMAINS = ["light", "switch", "fan", "cover", "media_player"]
DEFAULT_EXCLUDE_ENTITIES: list[str] = []
# Multi-action: one press can confirm & run the top-N context actions as a batch
# (single ✅ does all). Off by default -> classic single-action behavior.
DEFAULT_MULTI_ACTION = False
DEFAULT_MAX_ACTIONS = 3
# Manual-only: learn only from state changes a person triggered (HA context has
# a user_id). Excludes automation/script-driven changes. Note: physical switch
# presses also lack a user_id, so they're excluded too. Off by default.
DEFAULT_MANUAL_ONLY = False

# Event the engine reacts to (fired by the webhook, the service, or you).
EVENT_TRIGGER = "instinct_trigger"

# iOS actionable-notification feedback event.
EVENT_MOBILE_ACTION = "mobile_app_notification_action"

# Service name: instinct.trigger
SERVICE_TRIGGER = "trigger"

# Where the learned feedback DB lives (config dir → survives HACS updates,
# which only replace custom_components/instinct/).
DB_FILENAME = "instinct.db"

ON_STATES = {"on", "open", "playing", "home", "heat", "cool", "auto"}
OFF_STATES = {"off", "closed", "idle", "paused", "away"}
# States that clearly mean "active" for the already-in-state guard.
ACTIVE_STATES = {"on", "open", "playing", "home"}
