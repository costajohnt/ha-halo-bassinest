"""Home Assistant layer tests. Need pytest-homeassistant-custom-component."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

MAC = "AA:BB:CC:DD:EE:FF"


class FakeClient:
    """Stands in for HaloClient; class attributes steer the next instance."""

    login_error: Exception | None = None
    fetch_error: Exception | None = None
    last: FakeClient | None = None

    def __init__(self, session: Any, email: str, password: str, client_id: str) -> None:
        self.email, self.password = email, password
        self.bassinets: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.connected = False
        self.on_auth_failed = None
        self.calls: list[tuple] = []
        self.soothe_error: Exception | None = None
        FakeClient.last = self

    async def login(self) -> str:
        if FakeClient.login_error:
            raise FakeClient.login_error
        return "jwt"

    async def fetch_bassinets(self) -> dict[str, dict[str, Any]]:
        if FakeClient.fetch_error:
            raise FakeClient.fetch_error
        self.bassinets = {MAC: {"id": 7, "mac": MAC, "device_name": "Poppy's bassinet"}}
        return self.bassinets

    async def connect(self) -> None:
        self.connected = True
        self.states = {MAC: {"bassinest_id": MAC, "bassinestConnected": 1, "nightLightState": 1}}

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))
        self.connected = False

    def add_listener(self, cb):
        return lambda: None

    def motion_running(self, mac: str) -> bool:
        s = self.states.get(mac, {})
        return bool(s.get("motionState") or s.get("vibrationState"))

    async def soothe(self, mac: str, on: bool) -> None:
        if self.soothe_error:
            raise self.soothe_error
        self.calls.append(("soothe", on))

    async def set_control(self, mac: str, event: str, on: bool, **values: Any) -> None:
        self.calls.append(("set_control", event, on))

    async def turn_everything_off(self, mac: str) -> None:
        self.calls.append(("off",))


@pytest.fixture(autouse=True)
def fake_client(enable_custom_integrations):
    FakeClient.login_error = FakeClient.fetch_error = FakeClient.last = None
    with (
        patch("custom_components.halo_bassinest.HaloClient", FakeClient),
        patch("custom_components.halo_bassinest.config_flow.HaloClient", FakeClient),
    ):
        yield FakeClient
