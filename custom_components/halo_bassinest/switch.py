"""Soothing sound on/off (uses the tone and volume last set in the app)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .entity import HaloEntity

TONES = ["Brown Noise", "Pink Noise", "White Noise", "Heart Beat", "Stream"]


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    for mac in client.bassinets:
        add([HaloSound(client, mac, "sound"), HaloSoothe(client, mac, "soothe_2h")])


class HaloSound(HaloEntity, SwitchEntity):
    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get("soundState"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tone = self.state_data.get("sound") or 1
        return {
            "tone": TONES[tone - 1] if 1 <= tone <= 5 else tone,
            "volume": self.state_data.get("volume"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.client.set_control(self.mac, "SOUND", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.client.set_control(self.mac, "SOUND", False)


class HaloSoothe(HaloEntity, SwitchEntity):
    """Lowest rocking + vibration for 2 h; the bassinet's own timer ends it."""

    _attr_icon = "mdi:cradle"

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get("motionState") or self.state_data.get("vibrationState"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.client.soothe(self.mac, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.client.soothe(self.mac, False)
