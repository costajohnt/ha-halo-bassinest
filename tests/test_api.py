"""Wire-protocol check for api.py against a local fake HALO cloud.

Run: python tests/test_api.py   (needs aiohttp + python-socketio[asyncio_client])
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import aiohttp
import socketio
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "halo_bassinest"))
import api  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"


def make_jwt(exp: float) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc({'exp': int(exp)})}.sig"


async def main() -> None:
    logins: list[str] = []
    received: list[tuple[str, object]] = []
    auths: list[dict] = []
    sio = socketio.AsyncServer(async_mode="aiohttp")
    app = web.Application()
    sio.attach(app)

    async def login(request: web.Request) -> web.Response:
        body = await request.json()
        if body["password"] != "pw":
            return web.json_response({"message": "Invalid credentials"}, status=401)
        # First token is already expired, so the socket auth callable must re-login.
        token = make_jwt(time.time() - 10 if not logins else time.time() + 3600)
        logins.append(token)
        return web.json_response({"accessToken": token, "refreshToken": "r", "message": "ok"})

    async def account(request: web.Request) -> web.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return web.json_response(
            {"account": {"bassinests": [{"id": 7, "mac": MAC, "device_name": "Nest"}]}}
        )

    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/user/accountInformation", account)

    @sio.event
    async def connect(sid, environ, auth):
        auths.append(auth)

    @sio.on("DEVICE_SETTINGS_AND_STATES")
    async def snapshot(sid, *args):
        await sio.emit(
            "DEVICE_SETTINGS_AND_STATES",
            [{"bassinest_id": MAC, "bassinestConnected": 1, "nightLightState": 0,
              "nightLightBrightness": 50, "soundState": 0, "sound": 3, "volume": 2}],
            to=sid,
        )

    @sio.on("*")
    async def any_event(event, sid, data):
        received.append((event, data))
        # Echo like the real server: flat object keyed by mac.
        await sio.emit(event, json.loads(data)["data"], to=sid)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    api.REST_URL = api.SOCKET_URL = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        bad = api.HaloClient(session, "a@b.c", "nope", "ha-test")
        try:
            await bad.login()
            raise AssertionError("bad password accepted")
        except api.AuthError as err:
            assert "Invalid credentials" in str(err)

        client = api.HaloClient(session, "a@b.c", "pw", "ha-test")
        await client.fetch_bassinets()  # login #1 hands out an expired token
        await client.connect()
        for _ in range(50):
            if MAC in client.states:
                break
            await asyncio.sleep(0.05)

        assert client.connected
        assert auths[-1] == {"token": logins[-1]}, "socket auth must be raw JWT of a fresh login"
        assert api.jwt_exp(logins[-1]) > time.time(), "expired token was not refreshed"
        assert client.states[MAC]["nightLightBrightness"] == 50

        await client.set_control(MAC, "NIGHT_LIGHT", True, brightness=75)
        await client.set_control(MAC, "SOUND", True)
        await asyncio.sleep(0.3)
        assert client.states[MAC]["nightLightState"] == 1, "echoed push not merged"
        await client.turn_everything_off(MAC)  # triggers a fresh snapshot request
        await asyncio.sleep(0.3)
        assert client.states[MAC]["nightLightState"] == 0, "snapshot not re-requested"

        events = {e: json.loads(d) for e, d in received}
        assert all(isinstance(d, str) for _, d in received), "payloads must be JSON strings"
        light = events["NIGHT_LIGHT"]
        assert light["action"] == "NIGHT_LIGHT"
        d = light["data"]
        assert d["mac"] == MAC and d["bassinestId"] == 7 and d["device_id"] == "ha-test"
        assert d["nightLightState"] == 1 and d["previousNightLightState"] == 0
        assert d["nightLightBrightness"] == 75 and d["nightLightRuntime"] == 30
        snd = events["SOUND"]["data"]
        assert snd["soundState"] == 1 and snd["sound"] == 3 and snd["volume"] == 2
        assert events["TURN_EVERYTHING_OFF"]["data"]["turnEveryThingOff"] is True

        await client.disconnect()
    await runner.cleanup()
    print("ok")


def test_wire_protocol() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
