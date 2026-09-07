"""Config & options flow for Instinct."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_AUTO_CONFIDENCE,
    CONF_AUTO_MIN_SAMPLES,
    CONF_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_EXCLUDE_REACTIVE,
    CONF_HISTORY_DAYS,
    CONF_MANUAL_ONLY,
    CONF_MAX_ACTIONS,
    CONF_MIN_SCORE,
    CONF_MULTI_ACTION,
    CONF_NOTIFY_SERVICE,
    CONF_TIME_WINDOW_MINUTES,
    CONF_WEBHOOK_ID,
    DEFAULT_AUTO_CONFIDENCE,
    DEFAULT_AUTO_MIN_SAMPLES,
    DEFAULT_DOMAINS,
    DEFAULT_EXCLUDE_REACTIVE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MANUAL_ONLY,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MIN_SCORE,
    DEFAULT_MULTI_ACTION,
    DEFAULT_TIME_WINDOW_MINUTES,
    DOMAIN,
)


class InstinctConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Single instance is plenty.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            notify = user_input[CONF_NOTIFY_SERVICE].strip()
            if "." not in notify:
                errors[CONF_NOTIFY_SERVICE] = "invalid_notify"
            else:
                return self.async_create_entry(
                    title="Instinct",
                    data={
                        CONF_NOTIFY_SERVICE: notify,
                        CONF_WEBHOOK_ID: webhook.async_generate_id(),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NOTIFY_SERVICE): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return InstinctOptionsFlow()


class InstinctOptionsFlow(OptionsFlow):
    """Tunables after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Normalize CSV fields into lists.
            data = dict(user_input)
            data[CONF_DOMAINS] = _csv(user_input.get(CONF_DOMAINS, ""))
            data[CONF_EXCLUDE_ENTITIES] = _csv(
                user_input.get(CONF_EXCLUDE_ENTITIES, "")
            )
            return self.async_create_entry(title="", data=data)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_HISTORY_DAYS,
                    default=opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
                ): vol.All(int, vol.Range(min=1, max=365)),
                vol.Optional(
                    CONF_TIME_WINDOW_MINUTES,
                    default=opts.get(
                        CONF_TIME_WINDOW_MINUTES, DEFAULT_TIME_WINDOW_MINUTES
                    ),
                ): vol.All(int, vol.Range(min=5, max=240)),
                vol.Optional(
                    CONF_MIN_SCORE,
                    default=opts.get(CONF_MIN_SCORE, DEFAULT_MIN_SCORE),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_AUTO_CONFIDENCE,
                    default=opts.get(
                        CONF_AUTO_CONFIDENCE, DEFAULT_AUTO_CONFIDENCE
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
                vol.Optional(
                    CONF_AUTO_MIN_SAMPLES,
                    default=opts.get(
                        CONF_AUTO_MIN_SAMPLES, DEFAULT_AUTO_MIN_SAMPLES
                    ),
                ): vol.All(int, vol.Range(min=1, max=100)),
                vol.Optional(
                    CONF_MANUAL_ONLY,
                    default=opts.get(CONF_MANUAL_ONLY, DEFAULT_MANUAL_ONLY),
                ): bool,
                vol.Optional(
                    CONF_EXCLUDE_REACTIVE,
                    default=opts.get(
                        CONF_EXCLUDE_REACTIVE, DEFAULT_EXCLUDE_REACTIVE
                    ),
                ): bool,
                vol.Optional(
                    CONF_MULTI_ACTION,
                    default=opts.get(CONF_MULTI_ACTION, DEFAULT_MULTI_ACTION),
                ): bool,
                vol.Optional(
                    CONF_MAX_ACTIONS,
                    default=opts.get(CONF_MAX_ACTIONS, DEFAULT_MAX_ACTIONS),
                ): vol.All(int, vol.Range(min=1, max=10)),
                vol.Optional(
                    CONF_DOMAINS,
                    default=", ".join(opts.get(CONF_DOMAINS, DEFAULT_DOMAINS)),
                ): str,
                vol.Optional(
                    CONF_EXCLUDE_ENTITIES,
                    default=", ".join(opts.get(CONF_EXCLUDE_ENTITIES, [])),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _csv(value) -> list[str]:
    if isinstance(value, list):
        return value
    return [v.strip() for v in str(value).split(",") if v.strip()]
