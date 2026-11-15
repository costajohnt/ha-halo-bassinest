"""HALO BassiNest Smart Swivel Sleeper."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthError, HaloClient

_LOGGER = logging.getLogger(__name__)
DOMAIN = "halo_bassinest"
CONF_CLIENT_ID = "client_id"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]
# Read-only sensors from 0.1-0.3, replaced by switches of the same name in 0.4.
RETIRED = ("motion", "vibration", "autosoothe")

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
        for mac in client.bassinets:  # before connect(): nothing to leak if this is cancelled
            try:
                await client.fetch_presets(mac)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("HALO preset list unavailable, no preset picker: %s", err)
        await client.connect()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        raise ConfigEntryNotReady(str(err)) from err

    reg = er.async_get(hass)
    for mac in client.bassinets:
        for key in RETIRED:
            if entity_id := reg.async_get_entity_id("binary_sensor", DOMAIN, f"{mac}_{key}"):
                reg.async_remove(entity_id)

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
