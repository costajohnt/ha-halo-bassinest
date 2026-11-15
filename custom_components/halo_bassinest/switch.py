"""On/off controls: rocking, vibration, sound, AutoSoothe, soothing preset, Soothe 2h."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .api import PREFIX, SOOTHE_LEVEL
from .entity import HaloEntity

PARALLEL_UPDATES = 0  # push-only; emits are already sequential on one socket

CONTROLS = {"rocking": "MOTION", "vibration": "VIBRATION", "sound": "SOUND"}


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    entities: list[SwitchEntity] = []
    for mac in client.bassinets:
        entities += [HaloControl(client, mac, key) for key in CONTROLS]
        entities += [
            HaloPreset(client, mac, "autosoothe"),
            HaloPreset(client, mac, "soothing_preset"),
            HaloSoothe(client, mac, "soothe_2h"),
        ]
    add(entities)


class HaloControl(HaloEntity, SwitchEntity):
    """Turns a control on with the level, tone, volume and timer currently set."""

    @property
    def _event(self) -> str:
        return CONTROLS[self._attr_translation_key]

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get(f"{PREFIX[self._event]}State"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(self.client.set_control(self.mac, self._event, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self.client.set_control(self.mac, self._event, False))


class HaloPreset(HaloEntity, SwitchEntity):
    """AutoSoothe (respond to crying) or the selected soothing preset."""

    @property
    def _field(self) -> str:
        auto = self._attr_translation_key == "autosoothe"
        return "soothingPresetAutoModeState" if auto else "soothingPresetState"

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get(self._field))

    async def _set(self, on: bool) -> None:
        if self._attr_translation_key == "autosoothe":
            await self._send(self.client.set_preset(self.mac, auto=on))
        else:
            await self._send(self.client.set_preset(self.mac, on=on))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class HaloSoothe(HaloEntity, SwitchEntity):
    """One-tap preset: low rocking + vibration for 2 h on the bassinet's own timer.

    The client also sends a stop 5 minutes after the device timer should have fired.
    """

    _attr_icon = "mdi:cradle"
    _starting = False

    def _active(self, p: str) -> bool:
        s = self.state_data
        return bool(s.get(f"{p}State")) and s.get(p, SOOTHE_LEVEL) == SOOTHE_LEVEL

    @property
    def is_on(self) -> bool:
        # What Soothe starts: both at the low level. Plain Rocking on Low is not Soothe.
        return self._active("motion") and self._active("vibration")

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Never restart the 2 h window: skip if any motion is running (any level, even
        # half on) or a start is in flight. Completing a pair takes an explicit off, then on.
        if self._starting or self.client.motion_running(self.mac):
            return
        self._starting = True
        try:
            await self._send(self.client.soothe(self.mac, True))
        finally:
            self._starting = False

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self.client.soothe(self.mac, False))
