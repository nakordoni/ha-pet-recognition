"""NKD Pet Recognition integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import PetRecognitionClient
from .const import (
    CONF_API_KEY,
    CONF_CAMERAS,
    CONF_INTERVAL,
    CONF_MAX_FAILURES,
    CONF_THRESHOLD,
    DEFAULT_INTERVAL,
    DEFAULT_MAX_FAILURES,
    DEFAULT_THRESHOLD,
    DOMAIN,
    SERVICE_ANALYZE,
)
from .coordinator import PetRecognitionManager

PLATFORMS = ["sensor", "binary_sensor"]

ANALYZE_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_ids})


def _opt(entry: ConfigEntry, key: str, default):
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = PetRecognitionClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY]
    )
    manager = PetRecognitionManager(
        hass,
        entry.entry_id,
        client,
        list(_opt(entry, CONF_CAMERAS, [])),
        int(_opt(entry, CONF_INTERVAL, DEFAULT_INTERVAL)),
        float(_opt(entry, CONF_THRESHOLD, DEFAULT_THRESHOLD)),
        int(_opt(entry, CONF_MAX_FAILURES, DEFAULT_MAX_FAILURES)),
        on_auth_failed=lambda: entry.async_start_reauth(hass),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    manager.async_start()

    if not hass.services.has_service(DOMAIN, SERVICE_ANALYZE):

        async def _analyze(call: ServiceCall) -> dict:
            out: dict = {}
            for entity_id in call.data["entity_id"]:
                mgr = next(
                    (
                        m
                        for m in hass.data.get(DOMAIN, {}).values()
                        if entity_id in m.cameras
                    ),
                    None,
                )
                if mgr is None:
                    raise HomeAssistantError(f"{entity_id} is not configured")
                res = await mgr.async_poll(entity_id)
                out[entity_id] = {
                    "label": res.label,
                    "confidence": res.confidence,
                    "labels": res.labels,
                }
            return out

        hass.services.async_register(
            DOMAIN,
            SERVICE_ANALYZE,
            _analyze,
            schema=ANALYZE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id).async_stop()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_ANALYZE)
    return ok
