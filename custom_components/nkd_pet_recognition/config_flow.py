"""Config + options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api_client import PetApiAuthError, PetApiError, async_validate_key
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
    MIN_INTERVAL,
)

_CAMERA_SEL = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="camera", multiple=True)
)
_INTERVAL_SEL = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=MIN_INTERVAL, max=3600, step=1, unit_of_measurement="s",
        mode=selector.NumberSelectorMode.BOX,
    )
)
_THRESH_SEL = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=1, step=0.01, mode=selector.NumberSelectorMode.SLIDER)
)
_FAIL_SEL = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=20, step=1, mode=selector.NumberSelectorMode.BOX)
)


def _common_schema(d: dict[str, Any]) -> dict:
    return {
        vol.Required(CONF_CAMERAS, default=d.get(CONF_CAMERAS, [])): _CAMERA_SEL,
        vol.Required(CONF_INTERVAL, default=d.get(CONF_INTERVAL, DEFAULT_INTERVAL)): _INTERVAL_SEL,
        vol.Required(CONF_THRESHOLD, default=d.get(CONF_THRESHOLD, DEFAULT_THRESHOLD)): _THRESH_SEL,
        vol.Required(CONF_MAX_FAILURES, default=d.get(CONF_MAX_FAILURES, DEFAULT_MAX_FAILURES)): _FAIL_SEL,
    }


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    d = dict(d)
    d[CONF_INTERVAL] = int(d[CONF_INTERVAL])
    d[CONF_MAX_FAILURES] = int(d[CONF_MAX_FAILURES])
    d[CONF_THRESHOLD] = float(d[CONF_THRESHOLD])
    return d


class PetRecognitionConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await async_validate_key(
                    async_get_clientsession(self.hass), user_input[CONF_API_KEY]
                )
            except PetApiAuthError:
                errors["base"] = "invalid_auth"
            except PetApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="NKD Pet Recognition", data=_clean(user_input))

        schema = vol.Schema(
            {vol.Required(CONF_API_KEY): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ), **_common_schema(user_input or {})}
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await async_validate_key(
                    async_get_clientsession(self.hass), user_input[CONF_API_KEY]
                )
            except PetApiAuthError:
                errors["base"] = "invalid_auth"
            except PetApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
        schema = vol.Schema({vol.Required(CONF_API_KEY): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PetRecognitionOptionsFlow()


class PetRecognitionOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_common_schema(current))
        )
