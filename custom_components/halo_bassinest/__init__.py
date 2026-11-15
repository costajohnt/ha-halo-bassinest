"""HALO BassiNest Smart Swivel Sleeper."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthError, HaloClient

DOMAIN = "halo_bassinest"
CONF_CLIENT_ID = "client_id"
PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.LIGHT, Platform.SWITCH]

type HaloConfigEntry = ConfigEntry[HaloClient]


async def async_setup_entry(hass: HomeAssistant, entry: HaloConfigEntry) -> bool:
    client = HaloClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_CLIENT_ID],
    )
    client.on_auth_failed = lambda: entry.async_start_reauth(hass)
    try:
        await client.fetch_bassinets()
        await client.connect()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = client
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await client.disconnect()  # don't leave a socket and reconnect loop behind
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaloConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.disconnect()
    return ok
