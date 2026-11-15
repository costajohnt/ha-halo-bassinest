"""Night, floor and back lights (brightness in 25% steps)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .api import PREFIX
from .entity import HaloEntity

LIGHTS = {"night_light": "NIGHT_LIGHT", "floor_light": "FLOOR_LIGHT", "back_light": "BACK_LIGHT"}


def to_halo(ha_brightness: int) -> int:
    """HA 1..255 -> HALO 25/50/75/100."""
    return min(100, max(25, round(ha_brightness / 255 * 4) * 25))


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    add(HaloLight(client, mac, key) for mac in client.bassinets for key in LIGHTS)


class HaloLight(HaloEntity, LightEntity):
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    @property
    def _event(self) -> str:
        return LIGHTS[self._attr_translation_key]

    @property
    def _prefix(self) -> str:
        return PREFIX[self._event]

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get(f"{self._prefix}State"))

    @property
    def brightness(self) -> int:
        return round((self.state_data.get(f"{self._prefix}Brightness") or 25) * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        values = {}
        if ATTR_BRIGHTNESS in kwargs:
            values["brightness"] = to_halo(kwargs[ATTR_BRIGHTNESS])
        await self._send(self.client.set_control(self.mac, self._event, True, **values))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self.client.set_control(self.mac, self._event, False))
