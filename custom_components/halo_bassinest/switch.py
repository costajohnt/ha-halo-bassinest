"""Soothing sound on/off (uses the tone and volume last set in the app)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .api import SOOTHE_LEVEL
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
        await self._send(self.client.set_control(self.mac, "SOUND", True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self.client.set_control(self.mac, "SOUND", False))


class HaloSoothe(HaloEntity, SwitchEntity):
    """Lowest rocking + vibration for 2 h; the bassinet's own timer ends it.

    Disabled by default: enabling it is a deliberate choice. The client also sends a
    stop 5 minutes after the device timer should have fired (HaloClient backstop).
    """

    _attr_icon = "mdi:cradle"
    _attr_entity_registry_enabled_default = False
    _starting = False

    def _active(self, p: str) -> bool:
        s = self.state_data
        return bool(s.get(f"{p}State")) and s.get(p, SOOTHE_LEVEL) == SOOTHE_LEVEL

    @property
    def is_on(self) -> bool:
        # On only at the soothe level: rocking at a higher level from the app is not
        # "Soothe (low)" and shows on the Rocking/Vibration sensors instead.
        return self._active("motion") or self._active("vibration")

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Never restart the 2 h window: skip if any motion is running (any level, even
        # half on) or a start is in flight. Completing a pair takes an explicit off, then on.
        s = self.state_data
        if self._starting or s.get("motionState") or s.get("vibrationState"):
            return
        self._starting = True
        try:
            await self._send(self.client.soothe(self.mac, True))
        finally:
            self._starting = False

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self.client.soothe(self.mac, False))
