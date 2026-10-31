"""Client for the HALO BassiNest cloud (REST login + socket.io control/state).

Protocol reverse-engineered from the Android app com.halosleep.smartnursery 1.0.7.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import aiohttp
import socketio

_LOGGER = logging.getLogger(__name__)

REST_URL = "https://sleepsure.halosleep.com:7096"
SOCKET_URL = "https://sleepsure.halosleep.com:7098"
TIMEOUT = aiohttp.ClientTimeout(total=15)
RECONNECT_DELAY = 1  # seconds, doubles each attempt until a connection holds RECONNECT_MAX
RECONNECT_MAX = 60
SNAPSHOT_RETRY = 2  # seconds, doubles; re-asks for the snapshot while no state has arrived

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

# Event name -> state field prefix used in payloads. MOTION and VIBRATION are
# deliberately absent: rocking stays read-only on an infant sleeper.
PREFIX = {
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
    except (IndexError, KeyError, TypeError, ValueError):
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
        self.on_auth_failed: Callable[[], None] | None = None  # HA hooks reauth here
        self._listeners: list[Callable[[], None]] = []
        self._closed = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._delay: float = RECONNECT_DELAY
        self._connected_at = 0.0
        self._attempts = 0
        # ponytail: reconnection=False, we own the reconnect loop below so a server
        # kick is retried, backoff is bounded and a rejected password stops it.
        self._sio = socketio.AsyncClient(reconnection=False, logger=False)
        self._register_handlers()

    async def login(self) -> str:
        async with self._session.post(
            f"{REST_URL}/api/auth/login",
            json={"email": self._email, "password": self._password},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status not in (401, 403):
                resp.raise_for_status()  # outage/5xx/429: retryable, not bad credentials
            try:
                body = await resp.json(content_type=None)
            except ValueError:  # HTML error page from a proxy/WAF
                body = {}
            if not isinstance(body, dict):
                body = {}
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
            timeout=TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        self.bassinets = {b["mac"]: b for b in body["account"]["bassinests"]}
        return self.bassinets

    # socket.io

    async def connect(self) -> None:
        # Login here, not inside socketio's auth callable: engineio wraps (and can
        # swallow a cancel of) anything raised from its connect handler.
        token = await self._valid_token()
        self._closed = False
        await self._sio.connect(SOCKET_URL, auth={"token": token}, transports=["websocket"])

    async def disconnect(self) -> None:
        self._closed = True
        for task in (self._reconnect_task, self._snapshot_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._reconnect_task = self._snapshot_task = None
        await self._sio.disconnect()

    async def _reconnect(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._delay)
            self._delay = min(self._delay * 2, RECONNECT_MAX)
            self._attempts += 1
            try:
                await self.connect()
            except AuthError as err:
                _LOGGER.error("HALO login rejected, not retrying: %s", err)
                if self.on_auth_failed:
                    self.on_auth_failed()
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("HALO reconnect failed (%s), retrying in %ss", err, self._delay)
                continue
            if self._closed:  # unloaded while the handshake swallowed our cancel
                await self._sio.disconnect()
                return
            if self.connected:
                _LOGGER.info("HALO cloud reconnected")
                return
            # kicked during the handshake: the disconnect handler saw us running, so loop

    async def _poll_snapshot(self) -> None:
        delay: float = SNAPSHOT_RETRY
        while self.connected and not self.states:
            with suppress(socketio.exceptions.BadNamespaceError):
                await self._sio.emit("DEVICE_SETTINGS_AND_STATES")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("HALO state listener failed")

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        async def connect() -> None:
            self.connected = True
            self._connected_at = time.monotonic()
            self._notify()
            self._snapshot_task = asyncio.create_task(self._poll_snapshot())

        @sio.event
        async def disconnect(reason: str) -> None:
            self.connected = False
            # Entities stay unavailable until the next snapshot: cached state is of
            # unknown age after a drop.
            self.states.clear()
            self._notify()
            if self._snapshot_task:
                self._snapshot_task.cancel()
            if self._closed or reason == sio.reason.CLIENT_DISCONNECT:
                return
            if time.monotonic() - self._connected_at > RECONNECT_MAX:
                self._delay = RECONNECT_DELAY  # it held: a fresh outage, not a kick loop
            _LOGGER.warning("HALO cloud disconnected (%s), reconnecting", reason)
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect())

        @sio.on("DEVICE_SETTINGS_AND_STATES")
        async def snapshot(items: Any) -> None:
            for item in _loads(items) or []:
                self._store(item)
            unknown = set(self.states) - set(self.bassinets)
            if unknown:
                _LOGGER.warning(
                    "Snapshot ids %s match no account MAC %s", unknown, list(self.bassinets)
                )
            self._notify()

        @sio.on("DEVICE_SETTING_AND_STATE")
        async def single(item: Any) -> None:
            self._store(_loads(item))
            self._notify()

        for event in (*CONTROL_EVENTS, "TURN_EVERYTHING_OFF"):
            sio.on(event, self._merge)

    def _store(self, item: Any) -> None:
        mac = item.get("bassinest_id") if isinstance(item, dict) else None
        if not mac:
            _LOGGER.warning("Ignoring HALO push without bassinest_id: %r", item)
            return
        self.states.setdefault(mac, {}).update(item)

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
        if not self.connected:
            raise ConnectionError("HALO cloud not connected")
        data = {"eventId": int(time.time() * 1000), **data}
        # The app sends the payload as a JSON *string*, not an object.
        await self._sio.emit(event, json.dumps({"action": event, "data": data}))

    async def set_control(self, mac: str, event: str, on: bool, **values: Any) -> None:
        """Toggle SOUND/*_LIGHT, keeping current settings. Motion is read-only by design."""
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
