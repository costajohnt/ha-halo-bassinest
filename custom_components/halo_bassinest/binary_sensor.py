"""Read-only status: online, rocking, vibration, AutoSoothe."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HaloConfigEntry
from .entity import HaloEntity

# key -> state field. ponytail: motion/vibration are deliberately read-only;
# HA automations driving infant rocking is a safety call, add switches only on purpose.
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

    @property
    def available(self) -> bool:
        return self.client.connected

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get("bassinestConnected"))
