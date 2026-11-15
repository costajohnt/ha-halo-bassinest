"""Base entity: one HA device per bassinet, pushed updates from the socket."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DOMAIN
from .api import HaloClient


class HaloEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: HaloClient, mac: str, key: str) -> None:
        self.client = client
        self.mac = mac
        info = client.bassinets[mac]
        self._attr_unique_id = f"{mac}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=info.get("device_name") or "BassiNest",
            manufacturer="HALO",
            model="BassiNest Smart Swivel Sleeper",
            sw_version=info.get("firmware_version"),
            hw_version=info.get("hardware_version"),
        )

    @property
    def state_data(self) -> dict[str, Any]:
        return self.client.states.get(self.mac, {})

    @property
    def available(self) -> bool:
        return self.client.connected and bool(self.state_data.get("bassinestConnected"))

    async def _send(self, command: Awaitable[None]) -> None:
        """Run a client command, surfacing cloud drops as a clean HA error."""
        try:
            await command
        except ConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.client.add_listener(self.async_write_ha_state))
