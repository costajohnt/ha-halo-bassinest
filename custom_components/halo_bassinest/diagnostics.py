"""Diagnostics: redacted account and live state, for bug reports."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HaloConfigEntry

REDACTED = "**REDACTED**"
# ponytail: substring match on lowercased keys, because the HALO schema is
# reverse-engineered: an unknown field that looks personal is hidden, not leaked.
SENSITIVE = (
    "mac", "id", "name", "email", "password", "token", "ssid", "wifi", "serial",
    "user", "owner", "phone", "address", "location", "lat", "lon", "zip", "birth",
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if any(s in str(k).lower() for s in SENSITIVE) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaloConfigEntry
) -> dict[str, Any]:
    client = entry.runtime_data
    # Lists, not MAC-keyed dicts: the MAC itself is personal.
    return {
        "connected": client.connected,
        "bassinets": [redact(b) for b in client.bassinets.values()],
        "states": [redact(s) for s in client.states.values()],
    }
