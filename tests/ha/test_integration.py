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
    assert ids[f"{MAC}_soothe_2h"].disabled_by is er.RegistryEntryDisabler.INTEGRATION
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
    er.async_get(hass).async_update_entity(soothe, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
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

    # rocking at level 3 from the app: Soothe shows off, and turn_on sends nothing
    client.sent.clear()
    client.push({"bassinest_id": MAC, "motionState": 1, "motion": 3, "vibrationState": 0})
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
