"""Config flow, setup error mapping, entities, Soothe guard and diagnostics."""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halo_bassinest import CONF_CLIENT_ID, DOMAIN, api
from custom_components.halo_bassinest.api import AuthError
from custom_components.halo_bassinest.diagnostics import async_get_config_entry_diagnostics

from .conftest import MAC, SNAPSHOT, FakeClient

DATA = {CONF_EMAIL: "Parent@Example.com", CONF_PASSWORD: "pw", CONF_CLIENT_ID: "ha-test"}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="parent@example.com", data=DATA)
    entry.add_to_hass(hass)
    return entry


async def _user_flow(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "Parent@Example.com", CONF_PASSWORD: "pw"}
    )


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await _user_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "parent@example.com"
    assert result["data"][CONF_CLIENT_ID].startswith("ha-")


@pytest.mark.parametrize(
    ("error", "code"), [(AuthError("no"), "invalid_auth"), (RuntimeError("down"), "cannot_connect")]
)
async def test_user_flow_errors(hass: HomeAssistant, error: Exception, code: str) -> None:
    FakeClient.login_error = error
    result = await _user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": code}


async def test_user_flow_rejects_duplicate(hass: HomeAssistant) -> None:
    _entry(hass)
    result = await _user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_setup_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    FakeClient.fetch_error = AuthError("password changed")
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_setup_outage_retries(hass: HomeAssistant) -> None:
    FakeClient.fetch_error = RuntimeError("cloud down")
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _ids(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, er.RegistryEntry]:
    reg = er.async_get(hass)
    return {e.unique_id: e for e in er.async_entries_for_config_entry(reg, entry.entry_id)}


async def test_setup_entities_push_and_unload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    ids = _ids(hass, entry)
    assert ids[f"{MAC}_soothe_2h"].disabled_by is None  # full control, nothing hidden
    for key in ("rocking", "vibration", "autosoothe", "soothing_preset", "rocking_level",
                "tone", "preset", "rocking_timer", "master_timer", "volume", "sensitivity"):
        assert f"{MAC}_{key}" in ids, key
    assert ids[f"{MAC}_online"].entity_category is EntityCategory.DIAGNOSTIC
    light = ids[f"{MAC}_night_light"].entity_id
    assert hass.states.get(light).state == "unavailable"  # no snapshot yet
    client = FakeClient.last
    client.push(SNAPSHOT)
    await hass.async_block_till_done()
    assert hass.states.get(light).state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert client.disconnected


async def test_soothe_through_services(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    soothe = _ids(hass, entry)[f"{MAC}_soothe_2h"].entity_id
    client = FakeClient.last
    client.push({**SNAPSHOT, "motionState": 0, "vibrationState": 0})
    await hass.async_block_till_done()
    assert hass.states.get(soothe).state == "off"

    def call(service: str):
        return hass.services.async_call(
            "switch", service, {"entity_id": soothe}, blocking=True
        )

    # two taps at once: one start, not two (no restart of the 2 h window)
    client.emit_gate = asyncio.Event()
    taps = [hass.async_create_task(call("turn_on")) for _ in range(2)]
    await asyncio.sleep(0.05)
    client.emit_gate.set()
    await asyncio.gather(*taps)
    starts = [e for e, d in client.sent if d[f"{api.PREFIX[e]}State"] == 1]
    assert starts == ["MOTION", "VIBRATION"]

    # rocking on High: Soothe shows off, and turn_on sends nothing
    client.sent.clear()
    client.push({"bassinest_id": MAC, "motionState": 1, "motion": 2, "vibrationState": 0})
    await hass.async_block_till_done()
    assert hass.states.get(soothe).state == "off"
    await call("turn_on")
    assert client.sent == []

    # a cloud drop mid-command surfaces as a clean HA error
    async def dropped(event, data):
        raise ConnectionError("HALO cloud not connected")

    client._emit = dropped
    with pytest.raises(HomeAssistantError):
        await call("turn_off")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_fail_closed(hass: HomeAssistant) -> None:
    FakeClient.bassinet = {
        "device_name": "Poppy's bassinet",
        "ip": "203.0.113.9",
        "timeZone": "America/Los_Angeles",
        "invitedBy": "grandma@example.org",
        "settings": json.dumps({"ssid": "HomeNet"}),
        "firmware_version": "1.2.3",
    }
    entry = await _setup(hass)
    FakeClient.last.push({**SNAPSHOT, "wanIp": "198.51.100.7", "note": "{\"baby\": \"Poppy\"}"})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in (MAC, "Poppy", "203.0.113.9", "198.51.100.7", "Los_Angeles",
                   "grandma", "HomeNet", "Example.com", "pw"):
        assert secret not in text, secret
    assert diag["bassinets"][0]["firmware_version"] == "1.2.3"
    assert "timeZone" in diag["bassinets"][0]["keys"]
    assert diag["states"][0]["nightLightState"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_when_not_loaded(hass: HomeAssistant) -> None:
    FakeClient.fetch_error = RuntimeError("cloud down")
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag == {"entry_state": "setup_retry"}


@pytest.mark.parametrize("key", ["motion", "vibration", "autosoothe"])
async def test_retired_sensors_removed(hass: HomeAssistant, key: str) -> None:
    entry = _entry(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create(
        "binary_sensor", DOMAIN, f"{MAC}_{key}", config_entry=entry
    ).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get(old) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_every_control_through_services(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    ids = {k.removeprefix(f"{MAC}_"): v.entity_id for k, v in _ids(hass, entry).items()}
    client = FakeClient.last
    client.push({**SNAPSHOT, "motion": 1, "motionState": 0, "soothingPresetId": "1",
                 "sensitivity": 2, "volume": 1, "sound": 1})
    await hass.async_block_till_done()
    assert hass.states.get(ids["preset"]).state == "Calm"
    assert hass.states.get(ids["rocking_level"]).state == "Low"

    async def call(domain, service, key, **data):
        await hass.services.async_call(
            domain, service, {"entity_id": ids[key], **data}, blocking=True
        )
        return client.sent[-1]

    e, d = await call("select", "select_option", "rocking_level", option="High")
    assert e == "MOTION" and d["motion"] == 2 and d["motionState"] == 0
    e, d = await call("number", "set_value", "rocking_timer", value=45)
    assert e == "MOTION" and (d["motionRuntime"], d["motionRuntimeState"]) == (45, 1)
    e, d = await call("switch", "turn_on", "rocking")
    assert e == "MOTION" and d["motionState"] == 1
    e, d = await call("select", "select_option", "tone", option="Heart Beat")
    assert e == "SOUND" and d["sound"] == 4
    e, d = await call("number", "set_value", "volume", value=3)
    assert e == "SOUND" and d["volume"] == 3
    e, d = await call("number", "set_value", "night_light_timer", value=600)
    assert e == "NIGHT_LIGHT" and d["nightLightRuntime"] == 600
    e, d = await call("number", "set_value", "master_timer", value=30)
    assert e == "GLOBAL_RUNTIME" and d["globalRuntimeTimer"] == 30
    e, d = await call("switch", "turn_on", "autosoothe")
    assert e == "SOOTHING_PRESET" and d["soothingPresetAutoModeState"] == 1
    e, d = await call("number", "set_value", "sensitivity", value=4)
    assert e == "SOOTHING_PRESET" and d["sensitivity"] == 4
    e, d = await call("select", "select_option", "preset", option="Mine")
    assert e == "SOOTHING_PRESET" and d["soothingPresetId"] == "9"
    e, d = await call("switch", "turn_on", "soothing_preset")
    assert e == "SOOTHING_PRESET" and d["soothingPresetState"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
