"""Soothing sound on/off (uses the tone and volume last set in the app)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import HaloConfigEntry
from .api import SOOTHE_LEVEL, SOOTHE_MINUTES
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


BACKSTOP = (SOOTHE_MINUTES + 5) * 60  # seconds; HA stops it if the bassinet timer didn't


class HaloSoothe(HaloEntity, SwitchEntity):
    """Lowest rocking + vibration for 2 h; the bassinet's own timer ends it.

    Disabled by default: enabling it is a deliberate choice. HA also sends a stop
    5 minutes after the device timer should have fired, in case HALO ignored it.
    """

    _attr_icon = "mdi:cradle"
    _attr_entity_registry_enabled_default = False
    _cancel_backstop: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool:
        # On only at the soothe level: rocking at a higher level from the app is not
        # "Soothe (low)" and shows on the Rocking/Vibration sensors instead.
        s = self.state_data
        return any(
            s.get(f"{p}State") and s.get(p, SOOTHE_LEVEL) == SOOTHE_LEVEL
            for p in ("motion", "vibration")
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.is_on:
            return  # don't restart the 2 h window
        await self._send(self.client.soothe(self.mac, True))
        self._clear_backstop()
        self._cancel_backstop = async_call_later(self.hass, BACKSTOP, self._backstop)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._clear_backstop()
        await self._send(self.client.soothe(self.mac, False))

    async def _backstop(self, _now: Any) -> None:
        self._cancel_backstop = None
        await self.client.soothe(self.mac, False)

    def _clear_backstop(self) -> None:
        if self._cancel_backstop:
            self._cancel_backstop()
            self._cancel_backstop = None

    async def async_will_remove_from_hass(self) -> None:
        self._clear_backstop()
