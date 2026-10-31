"""Config flow: HALO account email + password."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import CONF_CLIENT_ID, DOMAIN
from .api import AuthError, HaloClient

SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str})
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class HaloConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _try_login(self, email: str, password: str) -> dict[str, str]:
        """Return form errors, empty on success."""
        client = HaloClient(async_get_clientsession(self.hass), email, password, "")
        try:
            await client.login()
        except AuthError:
            return {"base": "invalid_auth"}
        except Exception:  # noqa: BLE001
            return {"base": "cannot_connect"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            errors = await self._try_login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={**user_input, CONF_CLIENT_ID: f"ha-{uuid.uuid4().hex[:16]}"},
                )
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            errors = await self._try_login(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )
