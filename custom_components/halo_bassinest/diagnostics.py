"""Diagnostics: redacted account and live state, for bug reports."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HaloConfigEntry

REDACTED = "**REDACTED**"
# Fail closed: the HALO schema is reverse-engineered, so any field could be personal.
# Every string value is hidden (that also covers nested JSON strings, IPs, emails and
# names); numbers and booleans, which carry the protocol state, are kept unless the
# key looks personal. Device records keep only their key names and these fields.
KEEP_STRINGS = {"firmware_version", "hardware_version"}
SENSITIVE = (
    "mac", "id", "name", "email", "password", "token", "ssid", "wifi", "serial",
    "user", "owner", "phone", "address", "location", "lat", "lon", "coord", "geo",
    "zip", "birth", "dob", "ip",
)


def redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if any(s in key.lower() for s in SENSITIVE):
        return REDACTED
    if isinstance(value, str):
        return value if key in KEEP_STRINGS else REDACTED
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaloConfigEntry
) -> dict[str, Any]:
    client = getattr(entry, "runtime_data", None)
    if client is None:  # setup failed or unloaded: exactly when bugs get filed
        return {"entry_state": entry.state.value}
    # Lists, not MAC-keyed dicts: the MAC itself is personal.
    return {
        "entry_state": entry.state.value,
        "connected": client.connected,
        "bassinets": [
            {"keys": sorted(b), **{k: b[k] for k in KEEP_STRINGS if k in b}}
            for b in client.bassinets.values()
        ],
        "states": [redact(s) for s in client.states.values()],
    }
