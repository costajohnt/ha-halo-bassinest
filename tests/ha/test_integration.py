"""Config flow, setup error mapping, entities, Soothe guard and diagnostics."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halo_bassinest import CONF_CLIENT_ID, DOMAIN
from custom_components.halo_bassinest.api import AuthError
from custom_components.halo_bassinest.diagnostics import async_get_config_entry_diagnostics
from custom_components.halo_bassinest.switch import HaloSoothe

from .conftest import MAC, FakeClient

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


async def test_setup_entities_and_unload(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    reg = er.async_get(hass)
    ids = {e.unique_id: e for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    assert ids[f"{MAC}_soothe_2h"].disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert ids[f"{MAC}_online"].entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(ids[f"{MAC}_night_light"].entity_id).state == "on"
    client = FakeClient.last
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ("disconnect",) in client.calls


async def test_soothe_guard_and_errors() -> None:
    client = FakeClient(None, "a@b.c", "pw", "x")
    await client.fetch_bassinets()
    switch = HaloSoothe(client, MAC, "soothe_2h")
    client.states[MAC] = {"motionState": 1, "motion": 3}  # app rocking at level 3
    await switch.async_turn_on()
    assert client.calls == []  # no restart of the 2 h window
    client.states[MAC] = {"motionState": 0, "vibrationState": 0}
    await switch.async_turn_on()
    assert client.calls == [("soothe", True)]
    client.soothe_error = ConnectionError("HALO cloud not connected")
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()


async def test_diagnostics_redacted(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert MAC not in text and "Poppy" not in text and "Example.com" not in text
    assert diag["connected"] is True
    assert diag["states"][0]["nightLightState"] == 1
