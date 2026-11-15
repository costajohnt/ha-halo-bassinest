"""Rocking and vibration level, sound tone, soothing preset."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .api import LEVELS, TONES
from .entity import HaloEntity

PARALLEL_UPDATES = 0  # push-only; emits are already sequential on one socket

LEVEL_SELECTS = {"rocking_level": "MOTION", "vibration_level": "VIBRATION"}


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    entities: list[SelectEntity] = []
    for mac in client.bassinets:
        entities += [HaloLevel(client, mac, key) for key in LEVEL_SELECTS]
        entities.append(HaloTone(client, mac, "tone"))
        if client.presets.get(mac):  # the preset list loads over REST; skip if it didn't
            entities.append(HaloPresetChoice(client, mac, "preset"))
    add(entities)


class HaloLevel(HaloEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(LEVELS.values())

    @property
    def _event(self) -> str:
        return LEVEL_SELECTS[self._attr_translation_key]

    @property
    def current_option(self) -> str | None:
        field = "motion" if self._event == "MOTION" else "vibration"
        return LEVELS.get(self.state_data.get(field))

    async def async_select_option(self, option: str) -> None:
        level = next(k for k, v in LEVELS.items() if v == option)
        await self._send(self.client.set_control(self.mac, self._event, level=level))


class HaloTone(HaloEntity, SelectEntity):
    _attr_options = list(TONES.values())

    @property
    def current_option(self) -> str | None:
        return TONES.get(self.state_data.get("sound"))

    async def async_select_option(self, option: str) -> None:
        tone = next(k for k, v in TONES.items() if v == option)
        await self._send(self.client.set_control(self.mac, "SOUND", sound=tone))


class HaloPresetChoice(HaloEntity, SelectEntity):
    @property
    def options(self) -> list[str]:
        return self.client.presets.get(self.mac, [])

    @property
    def current_option(self) -> str | None:
        current = self.state_data.get("soothingPresetId")
        return current if current in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self._send(self.client.set_preset(self.mac, preset_id=option))
