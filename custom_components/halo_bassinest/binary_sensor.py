"""Read-only status: online, rocking, vibration, AutoSoothe."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .entity import HaloEntity

PARALLEL_UPDATES = 0  # pushed state, nothing to poll

# key -> state field. Motion status only; the Soothe 2h switch is the sole motion control.
SENSORS = {
    "motion": "motionState",
    "vibration": "vibrationState",
    "autosoothe": "soothingPresetAutoModeState",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for mac in client.bassinets:
        entities.append(HaloOnline(client, mac, "online"))
        entities += [HaloFlag(client, mac, key) for key in SENSORS]
    add(entities)


class HaloFlag(HaloEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get(SENSORS[self._attr_translation_key]))


class HaloOnline(HaloEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return self.client.connected

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get("bassinestConnected"))
