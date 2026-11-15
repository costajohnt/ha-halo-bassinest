"""Home Assistant layer tests. Need pytest-homeassistant-custom-component."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.halo_bassinest import api  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
SNAPSHOT = {"bassinest_id": MAC, "bassinestConnected": 1, "nightLightState": 1}


class FakeClient(api.HaloClient):
    """Real HaloClient (soothe lock, backstop, listeners, state merge); only network I/O
    is replaced. Class attributes steer the next instance."""

    login_error: Exception | None = None
    fetch_error: Exception | None = None
    bassinet: dict[str, Any] = {}
    last: FakeClient | None = None

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.sent: list[tuple[str, dict]] = []
        self.emit_gate = None  # asyncio.Event to hold emits, set by a test
        self.disconnected = False
        FakeClient.last = self

    async def login(self) -> str:
        if FakeClient.login_error:
            raise FakeClient.login_error
        self._token = "jwt"
        return self._token

    async def fetch_bassinets(self) -> dict[str, dict[str, Any]]:
        await self._valid_token()
        if FakeClient.fetch_error:
            raise FakeClient.fetch_error
        self.bassinets = {MAC: {"id": 7, "mac": MAC, **FakeClient.bassinet}}
        return self.bassinets

    async def fetch_presets(self, mac: str) -> dict[str, str]:
        self.presets[mac] = {"1": "Calm", "9": "Mine"}
        return self.presets[mac]

    async def connect(self) -> None:
        self._closed = False
        self.connected = True  # state arrives later, via push()

    async def disconnect(self) -> None:
        self._closed = True
        for task in self._backstops.values():
            task.cancel()
        self._backstops.clear()
        self.connected = False
        self.disconnected = True

    def push(self, item: dict[str, Any]) -> None:
        """What the snapshot handler does with a server push."""
        self._snapshot_seen = True
        self._store(item)
        self._notify()

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if not self.connected:
            raise ConnectionError("HALO cloud not connected")
        if self.emit_gate:
            await self.emit_gate.wait()
        self.sent.append((event, data))


@pytest.fixture(autouse=True)
def fake_client(enable_custom_integrations):
    FakeClient.login_error = FakeClient.fetch_error = FakeClient.last = None
    FakeClient.bassinet = {"device_name": "Poppy's bassinet", "firmware_version": "1.2.3"}
    api._backstop_deadlines.clear()
    with (
        patch("custom_components.halo_bassinest.HaloClient", FakeClient),
        patch("custom_components.halo_bassinest.config_flow.HaloClient", FakeClient),
    ):
        yield FakeClient
    api._backstop_deadlines.clear()
