"""Cloud connectivity per bassinet (controls live on the switch platform)."""

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

async def async_setup_entry(
    hass: HomeAssistant, entry: HaloConfigEntry, add: AddEntitiesCallback
) -> None:
    client = entry.runtime_data
    add(HaloOnline(client, mac, "online") for mac in client.bassinets)


class HaloOnline(HaloEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return self.client.connected

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get("bassinestConnected"))
