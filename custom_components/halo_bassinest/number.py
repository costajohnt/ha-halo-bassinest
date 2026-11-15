"""Timers (0 = no timer), master timer, volume, cry sensitivity."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .api import LIGHT_MAX_RUNTIME, MAX_RUNTIME, PREFIX
from .entity import HaloEntity

PARALLEL_UPDATES = 0  # push-only; emits are already sequential on one socket

TIMERS = {
    "rocking_timer": "MOTION",
    "vibration_timer": "VIBRATION",
    "sound_timer": "SOUND",
    "night_light_timer": "NIGHT_LIGHT",
    "floor_light_timer": "FLOOR_LIGHT",
    "back_light_timer": "BACK_LIGHT",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    entities: list[NumberEntity] = []
    for mac in client.bassinets:
        entities += [HaloTimer(client, mac, key) for key in TIMERS]
        entities += [
            HaloMasterTimer(client, mac, "master_timer"),
            HaloVolume(client, mac, "volume"),
            HaloSensitivity(client, mac, "sensitivity"),
        ]
    add(entities)


class _Minutes(HaloEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class HaloTimer(_Minutes):
    """A control's own timer; 0 means it runs until turned off."""

    @property
    def _event(self) -> str:
        return TIMERS[self._attr_translation_key]

    @property
    def native_max_value(self) -> float:
        return MAX_RUNTIME.get(PREFIX[self._event], LIGHT_MAX_RUNTIME)

    @property
    def native_value(self) -> float | None:
        p = PREFIX[self._event]
        if self.state_data.get(f"{p}RuntimeState"):
            return self.state_data.get(f"{p}Runtime")
        return 0

    async def async_set_native_value(self, value: float) -> None:
        await self._send(self.client.set_control(self.mac, self._event, runtime=int(value)))


class HaloMasterTimer(_Minutes):
    _attr_native_max_value = MAX_RUNTIME["global"]

    @property
    def native_value(self) -> float | None:
        s = self.state_data
        return s.get("globalRuntimeTimer") if s.get("globalTimerState") else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._send(self.client.set_global_timer(self.mac, int(value)))


class HaloVolume(HaloEntity, NumberEntity):
    _attr_native_min_value = 1
    _attr_native_max_value = 3
    _attr_native_step = 1

    @property
    def native_value(self) -> float | None:
        return self.state_data.get("volume")

    async def async_set_native_value(self, value: float) -> None:
        await self._send(self.client.set_control(self.mac, "SOUND", volume=int(value)))


class HaloSensitivity(HaloEntity, NumberEntity):
    """How readily AutoSoothe responds to crying (1-4, as in the app)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 4
    _attr_native_step = 1

    @property
    def native_value(self) -> float | None:
        return self.state_data.get("sensitivity")

    async def async_set_native_value(self, value: float) -> None:
        await self._send(self.client.set_preset(self.mac, sensitivity=int(value)))
