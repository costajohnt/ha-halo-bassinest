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


def _capturing_client(fail: set[str] | None = None):
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    client.bassinets = {MAC: {"id": 7}}
    # Non-default saved settings for both prefixes: soothe must override them all.
    client.states = {
        MAC: {
            "motion": 3, "motionRuntime": 600, "motionRuntimeState": 0,
            "vibration": 4, "vibrationRuntime": 600, "vibrationRuntimeState": 0,
        }
    }
    sent: list[tuple[str, dict]] = []

    async def capture(event, data):
        if fail and event in fail and data[f"{api.PREFIX[event]}State"] == 1:
            raise ConnectionError("dropped")
        if fail and "off:" + event in fail and data[f"{api.PREFIX[event]}State"] == 0:
            raise ConnectionError("dropped")
        sent.append((event, data))

    client._emit = capture  # type: ignore[method-assign]
    return client, sent


def test_soothe_is_lowest_level_on_device_timer():
    client, sent = _capturing_client()
    asyncio.run(client.soothe(MAC, True))
    assert [e for e, _ in sent] == ["MOTION", "VIBRATION"]
    for e, d in sent:
        p = api.PREFIX[e]
        assert d[p] == 1 and d[f"{p}State"] == 1
        assert d[f"{p}Runtime"] == 120 and d[f"{p}RuntimeState"] == 1
    # kwargs cannot raise the level
    sent.clear()
    asyncio.run(client.set_control(MAC, "MOTION", True, motion=5))
    assert sent[0][1]["motion"] == 1


def test_soothe_off_sends_both_off():
    client, sent = _capturing_client()
    asyncio.run(client.soothe(MAC, False))
    assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [("MOTION", 0), ("VIBRATION", 0)]


def test_light_off_payload():
    client, sent = _capturing_client()
    asyncio.run(client.set_control(MAC, "NIGHT_LIGHT", False))
    assert sent[0][1]["nightLightState"] == 0


def test_soothe_failed_start_rolls_back():
    client, sent = _capturing_client(fail={"VIBRATION"})
    with pytest.raises(ConnectionError):
        asyncio.run(client.soothe(MAC, True))
    # motion went on, then both were sent off: never left half on
    assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [
        ("MOTION", 1), ("MOTION", 0), ("VIBRATION", 0)
    ]


def test_soothe_stop_tries_both_even_if_first_fails():
    client, sent = _capturing_client(fail={"off:MOTION"})
    with pytest.raises(ConnectionError):
        asyncio.run(client.soothe(MAC, False))
    assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [("VIBRATION", 0)]


@pytest.fixture(autouse=True)
def _clear_backstops():
    api._backstop_deadlines.clear()
    yield
    api._backstop_deadlines.clear()


def test_socket_refusal_drops_token_once_per_outage():  # audit 2026-11-14 M5
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    client._delay = 0
    calls = 0

    async def refused():
        nonlocal calls
        calls += 1
        client._token = "fresh"  # stands in for the re-login connect() would do
        client._closed = calls >= 2
        raise api.socketio.exceptions.ConnectionError("token rejected")

    client._token = "stale"
    client.connect = refused  # type: ignore[method-assign]
    asyncio.run(client._reconnect())
    # first refusal dropped the token; the second kept the fresh one (no login storm)
    assert calls == 2 and client._token == "fresh"


def test_network_error_keeps_token():  # adversarial review: no relogin per outage retry
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    client._delay = 0

    async def offline():
        client._closed = True
        raise aiohttp.ClientConnectionError("offline")

    client._token = "kept"
    client.connect = offline  # type: ignore[method-assign]
    asyncio.run(client._reconnect())
    assert client._token == "kept"


def test_backstop_armed_on_start_and_cleared_by_stop():
    async def body():
        client, sent = _capturing_client()
        await client.soothe(MAC, True)
        assert MAC in api._backstop_deadlines and not client._backstops[MAC].done()
        task = client._backstops[MAC]
        await client.soothe(MAC, False)
        await asyncio.sleep(0)
        assert MAC not in api._backstop_deadlines and task.cancelled()

    run(body)


def test_backstop_stops_and_retries_when_cloud_down(monkeypatch):
    monkeypatch.setattr(api, "BACKSTOP", 0.05)
    monkeypatch.setattr(api, "BACKSTOP_RETRY", 0.05)

    async def body():
        fail = {"off:MOTION"}
        client, sent = _capturing_client(fail=fail)
        await client.soothe(MAC, True)
        await asyncio.sleep(0.12)  # deadline passed, stop failed on MOTION
        assert MAC in api._backstop_deadlines
        offs = [e for e, d in sent if d[f"{api.PREFIX[e]}State"] == 0]
        assert offs[0] == "VIBRATION"  # stop still tried the other control
        fail.clear()  # cloud back: the next retry succeeds and clears the deadline
        await asyncio.sleep(0.12)
        assert MAC not in api._backstop_deadlines
        assert ("MOTION", 0) in [(e, d["motionState"]) for e, d in sent if e == "MOTION"]

    run(body)


def test_backstop_survives_reload():
    async def body():
        old, _ = _capturing_client()
        await old.soothe(MAC, True)
        await old.disconnect()  # reload: tasks stop, deadline stays
        assert MAC in api._backstop_deadlines and not old._backstops
        new, sent = _capturing_client()
        api._backstop_deadlines[MAC] = 0  # overdue
        new._arm_backstop(MAC)  # what connect() does for each known bassinet
        await asyncio.sleep(0.01)
        assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [
            ("MOTION", 0),
            ("VIBRATION", 0),
        ]
        assert MAC not in api._backstop_deadlines

    run(body)


def test_cancel_mid_start_rolls_back():
    async def body():
        client, sent = _capturing_client()
        gate = asyncio.Event()
        capture = client._emit

        async def slow(event, data):
            if event == "VIBRATION" and data["vibrationState"] == 1:
                await gate.wait()  # never set: cancelled here
            await capture(event, data)

        client._emit = slow  # type: ignore[method-assign]
        task = asyncio.create_task(client.soothe(MAC, True))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)
        assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [
            ("MOTION", 1),
            ("MOTION", 0),
            ("VIBRATION", 0),
        ]

    run(body)


def test_malformed_snapshot_keeps_polling():  # audit 2026-11-14 L2
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    handler = client._sio.handlers["/"]["DEVICE_SETTINGS_AND_STATES"]
    asyncio.run(handler("{not json"))
    assert client._snapshot_seen is False
    asyncio.run(handler("null"))  # valid JSON, empty answer: not malformed
    assert client._snapshot_seen is True
    client._snapshot_seen = False
    asyncio.run(handler([]))
    assert client._snapshot_seen is True
