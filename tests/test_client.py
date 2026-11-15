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
                await asyncio.sleep(0.3)  # several snapshot retries: none answered
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
                await asyncio.sleep(1.0)  # 5+ retry windows at test delays
                assert len(cloud.logins) == n, "kept retrying login after rejection"
                assert failures == [1]
                assert not client.connected
            finally:
                await client.disconnect()

    run(body)


def test_reconnect_backs_off(monkeypatch):  # audit M3
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(api, "RECONNECT_MAX", 0.2)
    delays: list[float] = []  # delay the loop just slept, read at each attempt

    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            orig_connect = client.connect

            async def spy():
                delays.append(client._delay)  # already doubled for the next attempt
                await orig_connect()

            client.connect = spy  # type: ignore[method-assign]
            try:
                client._token = make_jwt(time.time() - 10)
                api.REST_URL = "http://127.0.0.1:1"  # login unreachable
                cloud.proxy.drop_all()
                await wait_for(lambda: len(delays) >= 4)
                assert delays[:4] == [0.1, 0.2, 0.2, 0.2], delays  # doubles, then capped
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
            "motion": 2, "motionRuntime": 90, "motionRuntimeState": 0, "motionState": 1,
            "vibration": 2, "vibrationRuntime": 90, "vibrationRuntimeState": 0,
            "vibrationState": 1,
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


def test_control_settings_payloads():
    client, sent = _capturing_client()
    asyncio.run(client.set_control(MAC, "MOTION", level=2, runtime=45))
    d = sent[-1][1]
    assert (d["motion"], d["motionRuntime"], d["motionRuntimeState"]) == (2, 45, 1)
    assert d["motionState"] == 1 and d["previousMotionState"] == 1  # on/off untouched
    assert d["previousMotionRuntimeState"] == 0  # the app's quirk: inverse of new state
    asyncio.run(client.set_control(MAC, "MOTION", runtime=0))  # timer off
    d = sent[-1][1]
    assert (d["motionRuntimeState"], d["motionRuntime"], d["previousMotionRuntimeState"]) == (
        0, 30, 1
    )
    asyncio.run(client.set_control(MAC, "SOUND", True, sound=4, volume=3, runtime=600))
    d = sent[-1][1]
    assert (d["sound"], d["volume"], d["soundRuntime"], d["soundState"]) == (4, 3, 600, 1)
    assert d["previousSoundRuntimeState"] == 0  # prior value: the quirk is motion-only
    client.states[MAC].update(nightLightRuntime=600, nightLightRuntimeState=0)
    asyncio.run(client.set_control(MAC, "NIGHT_LIGHT", True))
    assert sent[-1][1]["nightLightRuntime"] == 30  # timer off: the app's default
    asyncio.run(client.set_control(MAC, "BACK_LIGHT", brightness=50, runtime=600))
    assert sent[-1][1]["backLightBrightness"] == 50


@pytest.mark.parametrize(
    ("event", "kwargs"),
    [
        ("MOTION", {"level": 3}),
        ("VIBRATION", {"runtime": 121}),
        ("SOUND", {"volume": 4}),
        ("SOUND", {"sound": 6}),
        ("SOUND", {"runtime": 601}),
        ("NIGHT_LIGHT", {"brightness": 60}),
        ("MOTION", {"runtime": -1}),
    ],
)
def test_control_values_outside_app_range_are_refused(event, kwargs):
    client, sent = _capturing_client()
    with pytest.raises(ValueError):
        asyncio.run(client.set_control(MAC, event, **kwargs))
    assert sent == []


def test_global_timer_and_preset_payloads():
    client, sent = _capturing_client()
    client.states[MAC].update(soothingPresetId="12", sensitivity=3)
    asyncio.run(client.set_global_timer(MAC, 60))
    e, d = sent[-1]
    assert e == "GLOBAL_RUNTIME" and (d["globalTimerState"], d["globalRuntimeTimer"]) == (1, 60)
    assert "bassinestId" not in d
    asyncio.run(client.set_preset(MAC, auto=True))
    e, d = sent[-1]
    assert e == "SOOTHING_PRESET" and "bassinestId" not in d
    assert (d["soothingPresetAutoModeState"], d["previousSoothingPresetAutoModeState"]) == (1, 0)
    assert d["soothingPresetId"] == "12" and d["sensitivity"] == 3  # current kept
    asyncio.run(client.set_preset(MAC, preset_id="7", on=True, sensitivity=4))
    d = sent[-1][1]
    assert (d["soothingPresetId"], d["soothingPresetState"], d["sensitivity"]) == ("7", 1, 4)
    with pytest.raises(ValueError):
        asyncio.run(client.set_preset(MAC, sensitivity=5))
    with pytest.raises(ValueError):
        asyncio.run(client.set_global_timer(MAC, 121))


def test_fetch_presets_and_default():
    async def body():
        async with fake_cloud() as cloud:
            client = api.HaloClient(cloud.session, "a@b.c", "pw", "ha-test")
            # a custom preset named like a built-in one stays selectable
            assert await client.fetch_presets(MAC) == {"1": "Calm", "9": "Calm (9)"}
            client.bassinets = {MAC: {"id": 7}}
            sent: list[dict] = []

            async def capture(event, data):
                sent.append(data)

            client._emit = capture  # type: ignore[method-assign]
            await client.set_preset(MAC, on=True)  # no id in state: bassinet's own preset
            assert sent[-1]["soothingPresetId"] == "1"

    run(body)


def test_preset_guards():
    client, sent = _capturing_client()
    with pytest.raises(ValueError):  # nothing chosen and no default known
        asyncio.run(client.set_preset(MAC, on=True))
    with pytest.raises(ValueError):  # the app only allows this while AutoSoothe is on
        asyncio.run(client.set_preset(MAC, sensitivity=2))
    assert sent == []


def test_second_write_before_echo_keeps_first():  # review: stale "keep current"
    client, sent = _capturing_client()  # server state: rocking on
    asyncio.run(client.set_control(MAC, "MOTION", False))
    asyncio.run(client.set_control(MAC, "MOTION", level=1))  # before the echo
    assert sent[-1][1]["motionState"] == 0  # does not restart rocking
    assert client.states[MAC]["motionState"] == 1  # display waits for the server
    assert client.motion_running(MAC)  # the guard also reads server-confirmed state
    asyncio.run(client._merge({"mac": MAC, "motionState": 0}))  # echo
    assert "motionState" not in client._sent[MAC]


def test_null_fields_do_not_break_commands():
    client, sent = _capturing_client()
    client.states[MAC].update(motionRuntimeState=None, soothingPresetAutoModeState=None)
    asyncio.run(client.soothe(MAC, False))
    asyncio.run(client.set_global_timer(MAC, 0))
    assert len(sent) == 3


def test_pushes_that_change_other_controls_refresh_snapshot():
    client = api.HaloClient(None, "a@b.c", "pw", "ha-test")  # type: ignore[arg-type]
    asked: list[str] = []

    async def emit(event, *args, **kwargs):
        asked.append(event)

    client._sio.emit = emit  # type: ignore[method-assign]
    asyncio.run(client._merge({"mac": MAC, "turnEverythingOff": 1}))  # server spelling
    asyncio.run(client._merge({"mac": MAC, "soothingPresetState": 1}))
    asyncio.run(client._merge({"mac": MAC, "nightLightState": 1}))
    assert asked == ["DEVICE_SETTINGS_AND_STATES", "DEVICE_SETTINGS_AND_STATES"]


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

    seen: list[str | None] = []

    async def refused():
        nonlocal calls
        calls += 1
        seen.append(client._token)  # token this attempt would have logged in with
        client._token = "fresh"  # stands in for the re-login connect() would do
        client._closed = calls >= 3
        raise api.socketio.exceptions.ConnectionError("token rejected")

    client._token = "stale"
    client.connect = refused  # type: ignore[method-assign]
    asyncio.run(client._reconnect())
    # dropped after the first refusal only: one re-login per outage, no storm
    assert seen == ["stale", None, "fresh"]


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

        def offs():
            return [e for e, d in sent if d[f"{api.PREFIX[e]}State"] == 0]

        await wait_for(lambda: offs())  # deadline passed, stop failed on MOTION
        assert offs()[0] == "VIBRATION"  # stop still tried the other control
        assert MAC in api._backstop_deadlines
        fail.clear()  # cloud back: a retry succeeds and clears the deadline
        await wait_for(lambda: MAC not in api._backstop_deadlines)
        assert "MOTION" in offs()

    run(body)


def test_backstop_quiet_when_timer_already_stopped(monkeypatch):
    monkeypatch.setattr(api, "BACKSTOP", 0.05)

    async def body():
        client, sent = _capturing_client()
        await client.soothe(MAC, True)
        client.states[MAC].update(motionState=0, vibrationState=0)  # device timer fired
        client._snapshot_seen = True
        sent.clear()
        await wait_for(lambda: MAC not in api._backstop_deadlines)
        assert sent == []

    run(body)


def test_backstop_stops_when_state_unconfirmed(monkeypatch):  # fail closed
    monkeypatch.setattr(api, "BACKSTOP", 0.05)

    async def body():
        client, sent = _capturing_client()
        await client.soothe(MAC, True)
        client.states[MAC] = {"bassinest_id": MAC}  # keys missing
        client._snapshot_seen = True  # even with a fresh snapshot, missing keys fail closed
        sent.clear()
        await wait_for(lambda: MAC not in api._backstop_deadlines)
        assert [e for e, _ in sent] == ["MOTION", "VIBRATION"]

    run(body)


def test_backstop_stops_when_off_state_not_from_snapshot(monkeypatch):
    monkeypatch.setattr(api, "BACKSTOP", 0.05)

    async def body():
        client, sent = _capturing_client()
        await client.soothe(MAC, True)
        client.states[MAC].update(motionState=0, vibrationState=0)
        client._snapshot_seen = False  # e.g. after a reconnect, before the snapshot
        sent.clear()
        await wait_for(lambda: MAC not in api._backstop_deadlines)
        assert [e for e, _ in sent] == ["MOTION", "VIBRATION"]

    run(body)


def test_backstop_survives_reload():
    async def body():
        async with fake_cloud() as cloud:
            cloud.snapshot_items[0].update(motionState=1, motion=1)
            old = await connected_client(cloud)
            await old.soothe(MAC, True)
            await old.disconnect()  # reload: tasks stop, deadline stays
            assert MAC in api._backstop_deadlines and not old._backstops
            api._backstop_deadlines[MAC] = 0  # overdue by the time the new client connects
            cloud.received.clear()
            new = await connected_client(cloud)  # connect() re-arms
            await wait_for(lambda: MAC not in api._backstop_deadlines)
            assert [e for e, _ in cloud.received] == ["MOTION", "VIBRATION"]
            await new.disconnect()

    run(body)


def test_double_cancel_still_finishes_rollback():  # round-3 review
    async def body():
        client, sent = _capturing_client()
        never, gate = asyncio.Event(), asyncio.Event()
        capture = client._emit

        async def slow(event, data):
            on = data[f"{api.PREFIX[event]}State"]
            if event == "VIBRATION" and on:
                await never.wait()
            if event == "MOTION" and not on:
                await gate.wait()  # rollback in progress
            await capture(event, data)

        client._emit = slow  # type: ignore[method-assign]
        task = asyncio.create_task(client.soothe(MAC, True))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()  # second cancel lands while the rollback is mid-stop
        await asyncio.sleep(0.01)
        task.cancel()  # and a third
        await asyncio.sleep(0.01)
        assert not task.done() and client._soothe_lock(MAC).locked()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [
            ("MOTION", 1),
            ("MOTION", 0),
            ("VIBRATION", 0),
        ]

    run(body)


def test_stop_waits_for_inflight_start():  # round-2 review: no stop mid-start
    async def body():
        client, sent = _capturing_client()
        gate = asyncio.Event()
        capture = client._emit

        async def slow(event, data):
            if event == "VIBRATION" and data["vibrationState"] == 1:
                await gate.wait()
            await capture(event, data)

        client._emit = slow  # type: ignore[method-assign]
        start = asyncio.create_task(client.soothe(MAC, True))
        await asyncio.sleep(0.01)
        stop = asyncio.create_task(client.soothe(MAC, False))
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(start, stop)
        assert [(e, d[f"{api.PREFIX[e]}State"]) for e, d in sent] == [
            ("MOTION", 1),
            ("VIBRATION", 1),
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
    asyncio.run(handler('"5"'))  # parses, but not a list
    assert client._snapshot_seen is False
    asyncio.run(handler("null"))  # valid JSON, empty answer: not malformed
    assert client._snapshot_seen is True
    client._snapshot_seen = False
    asyncio.run(handler([]))
    assert client._snapshot_seen is True


def test_motion_running_blocks_retrigger_at_any_level():  # switch guard input
    client, _ = _capturing_client()
    client.states[MAC] = {"motionState": 1, "motion": 2}  # rocking on High
    assert client.motion_running(MAC)
    client.states[MAC] = {"vibrationState": 1}  # half on
    assert client.motion_running(MAC)
    client.states[MAC] = {"motionState": 0, "vibrationState": 0}
    assert not client.motion_running(MAC)


def test_state_follows_commands_without_echo():  # live 2026-11-15: no echo to sender
    async def body():
        async with fake_cloud() as cloud:
            cloud.echo = False
            client = await connected_client(cloud)
            try:
                await client.set_control(MAC, "FLOOR_LIGHT", True, brightness=75)
                await wait_for(lambda: client.states[MAC].get("floorLightState") == 1)
                assert client.states[MAC]["floorLightBrightness"] == 75
            finally:
                await client.disconnect()

    run(body)


def test_periodic_refresh_sees_device_side_changes(monkeypatch):  # timer ends unpushed
    monkeypatch.setattr(api, "STATE_REFRESH", 0.2)

    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            try:
                assert client.states[MAC]["nightLightState"] == 1
                cloud.snapshot_items[0]["nightLightState"] = 0  # its timer ran out
                await wait_for(lambda: client.states[MAC]["nightLightState"] == 0)
            finally:
                await client.disconnect()

    run(body)


def test_every_command_requests_an_ack():  # live 2026-11-15: cloud drops unacked sockets
    async def body():
        async with fake_cloud() as cloud:
            client = await connected_client(cloud)
            acks: list[tuple] = []
            client._on_ack = lambda *reply: acks.append(reply)  # type: ignore[method-assign]
            try:
                await client.set_global_timer(MAC, 5)
                await client.turn_everything_off(MAC)
                await wait_for(lambda: len(acks) == 2)
                assert acks[0] == ({"status": "ok"},)
            finally:
                await client.disconnect()

    run(body)
