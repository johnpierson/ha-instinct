"""The Instinct integration.

One button -> Instinct guesses what you want from your HA history and either
asks or (once proven) just does it.

Setup wires three entry points that all funnel into the engine:
  * a webhook  (POST /api/webhook/<id>)  — for the Apple Watch Shortcut
  * a service  (instinct.trigger)        — for automations / scripts
  * the event  instinct_trigger          — fire it however you like
"""

from __future__ import annotations

import logging

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .const import (
    CONF_WEBHOOK_ID,
    DOMAIN,
    EVENT_MOBILE_ACTION,
    EVENT_TRIGGER,
    SERVICE_DEBUG_CONTEXT,
    SERVICE_TRIGGER,
)
from .engine import InstinctEngine

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Instinct from a config entry."""
    engine = InstinctEngine(hass, entry)
    await engine.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = engine

    unsubs: list = []

    # --- event: instinct_trigger ------------------------------------------
    async def _on_event(event) -> None:
        await engine.async_handle_trigger()

    unsubs.append(hass.bus.async_listen(EVENT_TRIGGER, _on_event))

    # --- iOS actionable-notification feedback -----------------------------
    unsubs.append(
        hass.bus.async_listen(EVENT_MOBILE_ACTION, engine.handle_mobile_action)
    )

    # --- service: instinct.trigger ----------------------------------------
    async def _svc_trigger(call: ServiceCall) -> None:
        await engine.async_handle_trigger()

    hass.services.async_register(DOMAIN, SERVICE_TRIGGER, _svc_trigger)

    # --- service: instinct.debug_context (diagnostic) ---------------------
    async def _svc_debug(call: ServiceCall) -> None:
        seconds = int(call.data.get("seconds", 120))
        await engine.async_start_debug(seconds)

    hass.services.async_register(DOMAIN, SERVICE_DEBUG_CONTEXT, _svc_debug)

    # --- webhook ----------------------------------------------------------
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:

        async def _handle_webhook(hass_, webhook_id_, request):
            await engine.async_handle_trigger()

        try:
            webhook.async_register(
                hass,
                DOMAIN,
                "Instinct",
                webhook_id,
                _handle_webhook,
                local_only=False,
                allowed_methods=["POST", "PUT", "GET"],
            )
        except ValueError:
            # Already registered (e.g. reload) — ignore.
            pass
        unsubs.append(lambda: webhook.async_unregister(hass, webhook_id))
        _announce_webhook_url(hass, webhook_id)

    entry.async_on_unload(
        entry.add_update_listener(_async_reload_on_update)
    )
    hass.data[DOMAIN][f"{entry.entry_id}_unsubs"] = unsubs

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data.get(DOMAIN, {})
    for unsub in data.pop(f"{entry.entry_id}_unsubs", []):
        unsub()
    engine = data.pop(entry.entry_id, None)
    if engine is not None:
        engine.async_stop_debug()

    if not any(k for k in data if not k.endswith("_unsubs")):
        hass.services.async_remove(DOMAIN, SERVICE_TRIGGER)
        hass.services.async_remove(DOMAIN, SERVICE_DEBUG_CONTEXT)

    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _announce_webhook_url(hass: HomeAssistant, webhook_id: str) -> None:
    """Post the webhook URL to notifications so you can wire the Shortcut."""
    from homeassistant.components import persistent_notification
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    lines = ["Point your Apple Watch Shortcut at one of these URLs (POST):", ""]
    for external in (True, False):
        try:
            base = get_url(
                hass,
                allow_external=external,
                allow_internal=not external,
                prefer_external=external,
            )
        except NoURLAvailableError:
            continue
        label = "External" if external else "Internal"
        lines.append(f"**{label}:** `{base}/api/webhook/{webhook_id}`")

    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title="Instinct webhook",
        notification_id="instinct_webhook",
    )

