"""Turn everything off (motion, vibration, sound, lights)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .entity import HaloEntity

PARALLEL_UPDATES = 0  # push-only; emits are already sequential on one socket


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    add(HaloOff(client, mac, "turn_everything_off") for mac in client.bassinets)


class HaloOff(HaloEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self._send(self.client.turn_everything_off(self.mac))
