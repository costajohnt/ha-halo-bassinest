"""Reconnect edge cases from the adversarial review of PR #3 (zombie task, kick loops,
non-JSON 401, lost snapshot). Same fake cloud as test_client.py."""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp import web
from conftest import MAC, FakeCloud, api, connected_client, fake_cloud, make_jwt, wait_for


def run(coro_fn):
    asyncio.run(coro_fn())


def test_disconnect_during_inflight_login_leaves_zombie_reconnect(monkeypatch):
    """Unload while the reconnect task is inside login(): engineio swallows the
    CancelledError (engineio/async_client.py _trigger_event: `except CancelledError: pass`),
    so the loop survives disconnect() and reconnects to the cloud after unload."""
    gate = asyncio.Event()
    started = asyncio.Event()
    orig_login = FakeCloud._login

    async def slow_login(self, request):
        if self.logins:  # only the reconnect-time login is slow
            started.set()
            await gate.wait()
        return await orig_login(self, request)

    monkeypatch.setattr(FakeCloud, "_login", slow_login)
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.1)
    monkeypatch.setattr(api, "RECONNECT_MAX", 0.2)

    async def body():
        nonlocal gate, started
        gate = asyncio.Event()
        started = asyncio.Event()
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            client._token = make_jwt(time.time() - 10)  # force login on reconnect
            cloud.proxy.drop_all()
            await asyncio.wait_for(started.wait(), 5)  # reconnect is inside login()
            await client.disconnect()  # HA unload
            task_after = client._reconnect_task
            gate.set()
            await asyncio.sleep(2.5)
            assert task_after is None
            assert not client.connected, "client reconnected after disconnect()"
            assert len(cloud.sids) == 1, f"new cloud sessions after unload: {cloud.sids}"
            # no stray socket.io read loops / proxied conns left behind
            assert not cloud.proxy.conns, "websocket leaked after disconnect()"

    run(body)


def test_server_kick_on_accept_backs_off(monkeypatch):
    """Server accepts then immediately kicks (one-session-per-user fight with the phone app).
    Every cycle starts a fresh task with delay reset to RECONNECT_DELAY: no backoff."""
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.1)
    monkeypatch.setattr(api, "RECONNECT_MAX", 0.4)
    orig_connect = FakeCloud._connect

    async def kicking_connect(self, sid, environ, auth):
        await orig_connect(self, sid, environ, auth)
        if len(self.sids) > 1:
            async def kick():
                await asyncio.sleep(0.02)  # let the server register the session
                await self.sio.disconnect(sid)
            asyncio.get_running_loop().create_task(kick())

    monkeypatch.setattr(FakeCloud, "_connect", kicking_connect)

    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                await cloud.kick()
                await asyncio.sleep(2.0)
                n = len(cloud.sids)
                task = client._reconnect_task
                alive = task is not None and not task.done()
                # with 0.1 doubling to 0.4 cap: at most ~7 attempts in 2s; dead loop: 0 new
                assert alive or client.connected, f"reconnect loop dead: sids={n}"
                assert n <= 8, f"{n} sessions in 2s: no backoff across kick cycles"
            finally:
                await client.disconnect()

    run(body)


def test_login_401_with_non_json_body_is_auth_error():
    """A 401 whose body is not JSON (HTML error page from a proxy/WAF) must still be
    AuthError, else setup retries forever as ConfigEntryNotReady and no reauth is raised."""
    orig_login = FakeCloud._login

    async def html_401(self, request):
        if self.login_status == 401:
            return web.Response(status=401, text="<html>Unauthorized</html>")
        return await orig_login(self, request)

    async def body():
        FakeCloud._login = html_401
        try:
            async with fake_cloud() as cloud:
                client = api.HaloClient(cloud.session, "a@b.c", "pw", "ha-test")
                cloud.login_status = 401
                with pytest.raises(api.AuthError):
                    await client.login()
        finally:
            FakeCloud._login = orig_login

    run(body)


def test_reconnect_task_not_double_started_on_kick_during_connect(monkeypatch):
    """Server sends DISCONNECT in the same read as CONNECT ack: the disconnect handler
    sees the running reconnect task and skips; connect() then returns 'reconnected'
    and nobody reconnects again."""
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(api, "RECONNECT_MAX", 0.05)
    orig_connect = FakeCloud._connect

    async def kicking_connect(self, sid, environ, auth):
        await orig_connect(self, sid, environ, auth)
        if len(self.sids) == 2:
            async def kick():
                await asyncio.sleep(0)
                await self.sio.disconnect(sid)
            asyncio.get_running_loop().create_task(kick())

    monkeypatch.setattr(FakeCloud, "_connect", kicking_connect)

    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                await cloud.kick()
                await asyncio.sleep(1.5)
                task = client._reconnect_task
                assert client.connected or (task and not task.done()), (
                    f"stuck: connected={client.connected} task_done={task and task.done()} "
                    f"sids={len(cloud.sids)}"
                )
            finally:
                await client.disconnect()

    run(body)


def test_snapshot_request_is_retried_after_reconnect():
    """connect handler asks for DEVICE_SETTINGS_AND_STATES exactly once; if that one
    reply is lost, states stays {} and every entity is unavailable until the next drop."""

    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                cloud.answer_snapshot = False
                cloud.proxy.drop_all()
                await wait_for(lambda: not client.connected)
                await wait_for(lambda: client.connected)
                await asyncio.sleep(0.3)
                cloud.answer_snapshot = True  # server healthy again, answers any new request
                await asyncio.sleep(3)
                assert MAC in client.states, "no snapshot re-request: entities unavailable forever"
            finally:
                await client.disconnect()

    run(body)
