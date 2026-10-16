"""Client for the HALO BassiNest cloud (REST login + socket.io control/state).

Protocol reverse-engineered from the Android app com.halosleep.smartnursery 1.0.7.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp
import socketio

_LOGGER = logging.getLogger(__name__)

REST_URL = "https://sleepsure.halosleep.com:7096"
SOCKET_URL = "https://sleepsure.halosleep.com:7098"

# Per-control events the server pushes back as flat objects keyed by "mac".
CONTROL_EVENTS = (
    "MOTION",
    "VIBRATION",
    "SOUND",
    "NIGHT_LIGHT",
    "FLOOR_LIGHT",
    "BACK_LIGHT",
    "GLOBAL_RUNTIME",
    "SOOTHING_PRESET",
)

# Event name -> state field prefix used in payloads.
PREFIX = {
    "MOTION": "motion",
    "VIBRATION": "vibration",
    "SOUND": "sound",
    "NIGHT_LIGHT": "nightLight",
    "FLOOR_LIGHT": "floorLight",
    "BACK_LIGHT": "backLight",
}

DEFAULT_RUNTIME = 30  # minutes, the app's default when no timer is set


class AuthError(Exception):
    """Login rejected."""


def jwt_exp(token: str) -> int:
    """Return the exp claim of a JWT, 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError):
        return 0


class HaloClient:
    """One account: login, device list, live state per bassinet MAC."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        client_id: str,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._client_id = client_id  # our stable stand-in for the phone's ANDROID_ID
        self._token: str | None = None
        self.bassinets: dict[str, dict[str, Any]] = {}  # mac -> account Bassinest
        self.states: dict[str, dict[str, Any]] = {}  # mac -> live state
        self.connected = False
        self._listeners: list[Callable[[], None]] = []
        self._sio = socketio.AsyncClient(reconnection=True, logger=False)
        self._register_handlers()

    async def login(self) -> str:
        async with self._session.post(
            f"{REST_URL}/api/auth/login",
            json={"email": self._email, "password": self._password},
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400 or not body.get("accessToken"):
                raise AuthError(body.get("message", f"HTTP {resp.status}"))
        self._token = body["accessToken"]
        return self._token

    async def _valid_token(self) -> str:
        # No refresh endpoint is used by the app: re-login when within 5 min of exp.
        if not self._token or jwt_exp(self._token) - 300 < time.time():
            await self.login()
        return self._token

    async def fetch_bassinets(self) -> dict[str, dict[str, Any]]:
        token = await self._valid_token()
        async with self._session.get(
            f"{REST_URL}/api/user/accountInformation",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        self.bassinets = {b["mac"]: b for b in body["account"]["bassinests"]}
        return self.bassinets

    # socket.io

    async def _auth(self) -> dict[str, str]:
        return {"token": await self._valid_token()}

    async def connect(self) -> None:
        await self._sio.connect(SOCKET_URL, auth=self._auth, transports=["websocket"])

    async def disconnect(self) -> None:
        await self._sio.disconnect()

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        async def connect() -> None:
            self.connected = True
            await sio.emit("DEVICE_SETTINGS_AND_STATES")
            self._notify()

        @sio.event
        async def disconnect(*_: Any) -> None:
            self.connected = False
            self._notify()

        @sio.on("DEVICE_SETTINGS_AND_STATES")
        async def snapshot(items: Any) -> None:
            for item in _loads(items) or []:
                self.states[item["bassinest_id"]] = item
            self._notify()

        @sio.on("DEVICE_SETTING_AND_STATE")
        async def single(item: Any) -> None:
            item = _loads(item)
            self.states[item["bassinest_id"]] = item
            self._notify()

        for event in (*CONTROL_EVENTS, "TURN_EVERYTHING_OFF"):
            sio.on(event, self._merge)

    async def _merge(self, data: Any) -> None:
        data = _loads(data)
        mac = data.get("mac") if isinstance(data, dict) else None
        if not mac:
            return
        if data.get("turnEveryThingOff"):
            # Server also pushes per-control updates; ask for a fresh snapshot to be sure.
            await self._sio.emit("DEVICE_SETTINGS_AND_STATES")
            return
        self.states.setdefault(mac, {}).update(data)
        self._notify()

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        data = {"eventId": int(time.time() * 1000), **data}
        # The app sends the payload as a JSON *string*, not an object.
        await self._sio.emit(event, json.dumps({"action": event, "data": data}))

    async def set_control(self, mac: str, event: str, on: bool, **values: Any) -> None:
        """Toggle MOTION/VIBRATION/SOUND/*_LIGHT, keeping current settings."""
        p = PREFIX[event]
        state = self.states.get(mac, {})
        data: dict[str, Any] = {
            "mac": mac,
            "bassinestId": self.bassinets[mac]["id"],
            "device_id": self._client_id,
            f"{p}State": int(on),
            f"previous{p[0].upper()}{p[1:]}State": int(state.get(f"{p}State", 0)),
            f"{p}RuntimeState": int(state.get(f"{p}RuntimeState", 0)),
            f"{p}Runtime": state.get(f"{p}Runtime") or DEFAULT_RUNTIME,
            f"previous{p[0].upper()}{p[1:]}RuntimeState": int(state.get(f"{p}RuntimeState", 0)),
        }
        if event == "SOUND":
            data["sound"] = values.get("sound", state.get("sound") or 1)
            data["volume"] = values.get("volume", state.get("volume") or 1)
        elif event in ("MOTION", "VIBRATION"):
            data[p] = values.get(p, state.get(p) or 1)
        else:
            data[f"{p}Brightness"] = values.get(
                "brightness", state.get(f"{p}Brightness") or 25
            )
        await self._emit(event, data)

    async def turn_everything_off(self, mac: str) -> None:
        await self._emit("TURN_EVERYTHING_OFF", {"turnEveryThingOff": True, "mac": mac})


def _loads(value: Any) -> Any:
    """Pushes are normally objects, but accept JSON strings like the app sends."""
    return json.loads(value) if isinstance(value, str) else value
