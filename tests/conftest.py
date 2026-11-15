"""Fake HALO cloud for tests: aiohttp REST login + socket.io server behind a TCP proxy.

Never contacts the real device, the HALO cloud or Home Assistant. The proxy lets a
test drop the socket like a wifi outage (no close frame).
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
import socketio
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "halo_bassinest"))
import api  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def fast_delays(monkeypatch):
    """Production delays are seconds; tests that care about timing set their own."""
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(api, "RECONNECT_MAX", 0.2)
    monkeypatch.setattr(api, "SNAPSHOT_RETRY", 0.1)


def make_jwt(exp: float) -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'HS256'})}.{enc({'exp': exp})}.sig"


class Proxy:
    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.conns: list[asyncio.BaseTransport] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            r2, w2 = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.close()
            return
        self.conns += [writer.transport, w2.transport]

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                dst.transport.abort()

        await asyncio.gather(pump(reader, w2), pump(r2, writer))

    def drop_all(self) -> None:
        for t in self.conns:
            t.abort()
        self.conns.clear()

    async def stop(self) -> None:
        self.drop_all()
        self.server.close()
        await self.server.wait_closed()


class FakeCloud:
    def __init__(self) -> None:
        self.login_status = 200
        self.answer_snapshot = True
        self.snapshot_items: list[dict] = [
            {"bassinest_id": MAC, "bassinestConnected": 1, "nightLightState": 1}
        ]
        self.logins: list[float] = []
        self.sids: list[str] = []
        self.received: list[tuple[str, object]] = []
        self.sio = socketio.AsyncServer(async_mode="aiohttp")
        self.app = web.Application()
        self.sio.attach(self.app)
        self.app.router.add_post("/api/auth/login", self._login)
        self.app.router.add_get("/api/user/accountInformation", self._account)
        self.app.router.add_get("/api/soothingPresetInfo/{mac}", self._presets)
        self.sio.on("connect", self._connect)
        self.sio.on("DEVICE_SETTINGS_AND_STATES", self._snapshot)
        self.sio.on("*", self._echo)

    async def _login(self, request: web.Request) -> web.Response:
        self.logins.append(time.time())
        if self.login_status != 200:
            return web.json_response({"message": "nope"}, status=self.login_status)
        return web.json_response({"accessToken": make_jwt(time.time() + 3600)})

    async def _presets(self, request: web.Request) -> web.Response:
        assert request.match_info["mac"] == MAC
        return web.json_response(
            {
                "allPresets": [
                    {"id": 1, "soothingPresetName": "Calm"},
                    {"id": 9, "soothingPresetName": "Calm"},
                ],
                "presetAssociatedWithMac": {"soothingPresetId": "1"},
            }
        )

    async def _account(self, request: web.Request) -> web.Response:
        return web.json_response({"account": {"bassinests": [{"id": 7, "mac": MAC}]}})

    async def _connect(self, sid: str, environ: dict, auth: dict) -> None:
        self.sids.append(sid)

    async def _snapshot(self, sid: str, *args: object) -> None:
        if self.answer_snapshot:
            await self.sio.emit("DEVICE_SETTINGS_AND_STATES", self.snapshot_items, to=sid)

    async def _echo(self, event: str, sid: str, data: str) -> None:
        self.received.append((event, data))
        await self.sio.emit(event, json.loads(data)["data"], to=sid)

    async def kick(self) -> None:
        await self.sio.disconnect(self.sids[-1])


@asynccontextmanager
async def fake_cloud():
    cloud = FakeCloud()
    runner = web.AppRunner(cloud.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    proxy = Proxy(port)
    pport = await proxy.start()
    cloud.rest_url = api.REST_URL = f"http://127.0.0.1:{port}"
    api.SOCKET_URL = f"http://127.0.0.1:{pport}"
    cloud.proxy = proxy
    async with aiohttp.ClientSession() as session:
        cloud.session = session
        try:
            yield cloud
        finally:
            await proxy.stop()
            await runner.cleanup()


async def connected_client(cloud: FakeCloud) -> api.HaloClient:
    client = api.HaloClient(cloud.session, "a@b.c", "pw", "ha-test")
    await client.fetch_bassinets()
    await client.connect()
    await wait_for(lambda: MAC in client.states)
    return client


async def wait_for(cond, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)
