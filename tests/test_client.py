"""Reliability tests for api.HaloClient against the fake cloud in conftest.py.

Run: pytest tests   (needs aiohttp, python-socketio[asyncio_client], pytest)
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from conftest import MAC, api, connected_client, fake_cloud, make_jwt, wait_for


def run(coro_fn):
    asyncio.run(coro_fn())


def test_login_errors_are_classified():  # audit H1
    async def body():
        async with fake_cloud() as cloud:
            client = api.HaloClient(cloud.session, "a@b.c", "pw", "ha-test")
            cloud.login_status = 503
            with pytest.raises(aiohttp.ClientResponseError):
                await client.login()
            cloud.login_status = 401
            with pytest.raises(api.AuthError):
                await client.login()
            cloud.login_status = 403
            with pytest.raises(api.AuthError):
                await client.login()

    run(body)


def test_disconnect_clears_state_until_fresh_snapshot():  # audit H2
    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                cloud.answer_snapshot = False
                cloud.proxy.drop_all()
                await asyncio.wait_for(wait_for(lambda: not client.connected), 5)
                assert client.states == {}
                await wait_for(lambda: client.connected)
                await asyncio.sleep(0.3)
                assert client.states == {}, "pre-drop state must not come back as fresh"
                cloud.answer_snapshot = True
                await client._sio.emit("DEVICE_SETTINGS_AND_STATES")
                await wait_for(lambda: MAC in client.states)
            finally:
                await client.disconnect()

    run(body)


def test_server_disconnect_reconnects():  # audit M1
    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                await cloud.kick()
                await wait_for(lambda: not client.connected)
                await wait_for(lambda: client.connected and MAC in client.states)
                assert len(cloud.sids) == 2
            finally:
                await client.disconnect()

    run(body)


def test_rejected_login_stops_reconnect_and_reports():  # audit M3
    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            failures: list[int] = []
            client.on_auth_failed = lambda: failures.append(1)
            try:
                client._token = make_jwt(time.time() - 10)
                cloud.login_status = 401
                cloud.proxy.drop_all()
                await wait_for(lambda: failures)
                n = len(cloud.logins)
                await asyncio.sleep(2.5)
                assert len(cloud.logins) == n, "kept retrying login after rejection"
                assert failures == [1]
                assert not client.connected
            finally:
                await client.disconnect()

    run(body)


def test_reconnect_backs_off(monkeypatch):  # audit M3
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.1)
    monkeypatch.setattr(api, "RECONNECT_MAX", 0.4)

    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                client._token = make_jwt(time.time() - 10)
                api.REST_URL = "http://127.0.0.1:1"  # login unreachable
                cloud.proxy.drop_all()
                await asyncio.sleep(1.6)
                attempts = client._attempts
                assert 3 <= attempts <= 5, attempts  # 0.1, 0.2, 0.4, 0.4 ... not 16
                api.REST_URL = cloud.rest_url
                await wait_for(lambda: client.connected and MAC in client.states)
            finally:
                await client.disconnect()

    run(body)


def test_malformed_push_item_is_skipped_not_fatal():  # audit M2
    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            notifies: list[float] = []
            client.add_listener(lambda: notifies.append(time.time()))
            try:
                cloud.snapshot_items = [
                    {"no_id": True},
                    "garbage",
                    {"bassinest_id": MAC, "bassinestConnected": 1, "nightLightState": 0},
                ]
                await client._sio.emit("DEVICE_SETTINGS_AND_STATES")
                await wait_for(lambda: notifies)
                assert client.states[MAC]["nightLightState"] == 0
                assert client.connected
                # single: partial object must merge, not wipe the other fields
                await cloud.sio.emit(
                    "DEVICE_SETTING_AND_STATE", {"bassinest_id": MAC, "soundState": 1}
                )
                await wait_for(lambda: client.states[MAC].get("soundState") == 1)
                assert client.states[MAC]["bassinestConnected"] == 1
            finally:
                await client.disconnect()

    run(body)


def test_emit_while_disconnected_is_clean_error():  # audit L1
    async def body():
        async with fake_cloud() as cloud:
            client = api.HaloClient(cloud.session, "a@b.c", "pw", "ha-test")
            await client.fetch_bassinets()
            with pytest.raises(ConnectionError):
                await client.set_control(MAC, "NIGHT_LIGHT", True)

    run(body)


def test_jwt_exp_null():  # audit L2
    assert api.jwt_exp(make_jwt(None)) == 0  # type: ignore[arg-type]
    assert api.jwt_exp("not.a.jwt") == 0
    assert api.jwt_exp(make_jwt(123)) == 123


def test_failing_listener_does_not_block_others():  # audit L3
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    seen: list[int] = []

    def bad() -> None:
        raise RuntimeError("boom")

    client.add_listener(bad)
    client.add_listener(lambda: seen.append(1))
    client._notify()
    assert seen == [1]


def test_motion_is_not_controllable():  # audit L6
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    client.bassinets = {MAC: {"id": 7}}
    client.connected = True
    for event in ("MOTION", "VIBRATION"):
        with pytest.raises(KeyError):
            asyncio.run(client.set_control(MAC, event, True))
