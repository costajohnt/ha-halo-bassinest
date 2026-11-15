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
RECONNECT_DELAY = 1  # seconds, doubles each attempt
RECONNECT_MAX = 60
HOLD_RESET = 10  # a connection that held this long resets the backoff: outage, not a kick loop
SNAPSHOT_RETRY = 2  # seconds, doubles; re-asks for the snapshot while no state has arrived
# The cloud does not echo a command to the client that sent it, and does not push when a
# bassinet timer ends (verified live 2026-11-15), so state is re-read after every command
# and on a fixed interval.
STATE_REFRESH = 60  # seconds
REFRESH_AFTER_COMMAND = 1.0  # seconds, lets the cloud apply the command first

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
# Ranges from the app's controls (ControlFragment): motion/vibration 1 Low, 2 High;
# timers in minutes; sound tones 1-5; volume 1-3; light brightness in 25% steps.
LEVELS = {1: "Low", 2: "High"}
TONES = {1: "Brown Noise", 2: "Pink Noise", 3: "White Noise", 4: "Heart Beat", 5: "Stream"}
MAX_RUNTIME = {"motion": 120, "vibration": 120, "global": 120}  # sound, lights: 600
LIGHT_MAX_RUNTIME = 600
BRIGHTNESS = (25, 50, 75, 100)
SENSITIVITY = range(1, 5)
SOOTHE_LEVEL = 1  # lowest rocking/vibration level
SOOTHE_MINUTES = 120  # enforced by the bassinet itself, so it stops even if HA is down
BACKSTOP = (SOOTHE_MINUTES + 5) * 60  # seconds; HA sends a stop if the bassinet timer didn't
BACKSTOP_RETRY = 60  # seconds between stop attempts while the cloud is unreachable

# mac -> monotonic deadline of a soothe this process started. Module-level so a pending
# backstop survives an integration reload (a new HaloClient re-arms it on connect). An HA
# restart loses it; the bassinet's own timer is the primary stop.
_backstop_deadlines: dict[str, float] = {}
_MALFORMED = object()


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
        self._snapshot_seen = False
        self._token_dropped = False  # once per outage, see _reconnect
        self._backstops: dict[str, asyncio.Task[None]] = {}
        # mac -> preset names. The cloud identifies presets by name: soothingPresetId in
        # state and in SOOTHING_PRESET is the name (seen live 2026-11-15), not the REST id.
        self.presets: dict[str, list[str]] = {}
        self._default_preset: dict[str, str] = {}  # mac -> preset tied to the bassinet
        self._sent: dict[str, dict[str, Any]] = {}  # mac -> fields sent, not yet echoed
        self._refresh_now = asyncio.Event()
        # Serialises start, stop and the backstop per bassinet, so a stop can't land in
        # the middle of a start or clear a deadline a newer start just set.
        self._soothe_locks: dict[str, asyncio.Lock] = {}
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
        if self._closed:  # unloaded while the handshake swallowed our cancel
            return
        for mac in self.bassinets:
            if mac in _backstop_deadlines:
                self._arm_backstop(mac)

    async def disconnect(self) -> None:
        self._closed = True
        try:
            # Backstop tasks stop with this client; their deadlines stay for the next one.
            for task in (self._reconnect_task, self._snapshot_task, *self._backstops.values()):
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        if (me := asyncio.current_task()) and me.cancelling():
                            raise  # it was us being cancelled, not the task
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("HALO background task failed")
        finally:
            for task in self._backstops.values():  # anything armed while we awaited
                task.cancel()
            self._reconnect_task = self._snapshot_task = None
            self._backstops.clear()
            await self._sio.disconnect()

    async def _reconnect(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._delay)
            self._delay = min(self._delay * 2, RECONNECT_MAX)
            try:
                await self.connect()
            except AuthError as err:
                _LOGGER.error("HALO login rejected, not retrying: %s", err)
                try:
                    if self.on_auth_failed:
                        self.on_auth_failed()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("HALO reauth callback failed")
                return
            except Exception as err:  # noqa: BLE001
                if isinstance(err, socketio.exceptions.ConnectionError):
                    self._drop_token_once()
                _LOGGER.warning("HALO reconnect failed (%s), retrying in %ss", err, self._delay)
                continue
            if self._closed:  # unloaded while the handshake swallowed our cancel
                await self._sio.disconnect()
                return
            if self.connected:
                _LOGGER.info("HALO cloud reconnected")
                self._token_dropped = False
                return
            # kicked during the handshake: the disconnect handler saw us running, so loop
            self._drop_token_once()

    def _drop_token_once(self) -> None:
        """A token revoked before exp is refused by the socket, never by login().

        Drop it once per outage so the next attempt logs in fresh (and a changed password
        reaches AuthError/reauth), without logging in on every retry of a long outage.
        """
        if not self._token_dropped:
            self._token_dropped = True
            self._token = None

    async def _poll_snapshot(self) -> None:
        """Ask for the full snapshot: with backoff until one arrives, then every
        STATE_REFRESH seconds, and shortly after each command we send."""
        delay: float = SNAPSHOT_RETRY
        while self.connected:
            with suppress(socketio.exceptions.BadNamespaceError):
                await self._sio.emit("DEVICE_SETTINGS_AND_STATES")
            self._refresh_now.clear()
            wait = STATE_REFRESH if self._snapshot_seen else delay
            try:
                await asyncio.wait_for(self._refresh_now.wait(), wait)
                await asyncio.sleep(REFRESH_AFTER_COMMAND)
            except TimeoutError:
                if not self._snapshot_seen:
                    delay = min(delay * 2, RECONNECT_MAX)

    def refresh_soon(self) -> None:
        """Re-read state shortly: our own commands are not echoed back to us."""
        self._refresh_now.set()

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
            self._sent.clear()
            self._snapshot_seen = False
            self._notify()
            if self._snapshot_task:
                self._snapshot_task.cancel()
            if self._closed or reason == sio.reason.CLIENT_DISCONNECT:
                return
            if time.monotonic() - self._connected_at > HOLD_RESET:
                self._delay = RECONNECT_DELAY
            _LOGGER.warning("HALO cloud disconnected (%s), reconnecting", reason)
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect())

        @sio.on("DEVICE_SETTINGS_AND_STATES")
        async def snapshot(items: Any) -> None:
            parsed = _loads(items)
            if parsed is _MALFORMED or not isinstance(parsed, list | None):
                _LOGGER.warning("Ignoring malformed HALO snapshot")
                return  # keep polling for a good snapshot
            self._snapshot_seen = True
            for item in parsed or []:
                self._store(item)
            unknown = set(self.states) - set(self.bassinets)
            if unknown:
                _LOGGER.warning("Snapshot has %d id(s) matching no account MAC", len(unknown))
                _LOGGER.debug("Unmatched ids %s, account MACs %s", unknown, list(self.bassinets))
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
            _LOGGER.warning("Ignoring HALO push without bassinest_id")
            _LOGGER.debug("Push without bassinest_id: %r", item)
            return
        self.states.setdefault(mac, {}).update(item)
        self._confirmed(mac, item)

    async def _merge(self, data: Any) -> None:
        data = _loads(data)
        mac = data.get("mac") if isinstance(data, dict) else None
        if not mac:
            return
        # The app emits turnEveryThingOff; the server pushes turnEverythingOff.
        if data.get("turnEverythingOff") or data.get("turnEveryThingOff"):
            # Server also pushes per-control updates; ask for a fresh snapshot to be sure.
            with suppress(socketio.exceptions.BadNamespaceError):
                await self._sio.emit("DEVICE_SETTINGS_AND_STATES")
            return
        self.states.setdefault(mac, {}).update(data)
        self._confirmed(mac, data)
        self._notify()
        if "soothingPresetState" in data:
            # A preset switches other controls server-side; re-read once it has.
            with suppress(socketio.exceptions.BadNamespaceError):
                await self._sio.emit("DEVICE_SETTINGS_AND_STATES")

    def _on_ack(self, *reply: Any) -> None:
        _LOGGER.debug("HALO ack %r", reply)

    def _confirmed(self, mac: str, item: dict[str, Any]) -> None:
        sent = self._sent.get(mac)
        if sent:
            for key in item:
                sent.pop(key, None)

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if not self.connected:
            raise ConnectionError("HALO cloud not connected")
        data = {"eventId": int(time.time() * 1000), **data}
        # The app sends the payload as a JSON *string*, not an object.
        try:
            # Always ask for an ack, like the app. Some cloud handlers reply
            # unconditionally; without an ack request the cloud drops the socket
            # (seen live 2026-11-15 on GLOBAL_RUNTIME, Soothe off and preset stop).
            await self._sio.emit(
                event, json.dumps({"action": event, "data": data}), callback=self._on_ack
            )
        except socketio.exceptions.SocketIOError as err:  # dropped between check and send
            raise ConnectionError(f"HALO cloud not connected ({err})") from err

    async def set_control(
        self,
        mac: str,
        event: str,
        on: bool | None = None,
        *,
        level: int | None = None,
        runtime: int | None = None,
        brightness: int | None = None,
        sound: int | None = None,
        volume: int | None = None,
    ) -> None:
        """Send one control with the app's full field set; unset values keep current state.

        runtime is the timer in minutes, 0 turns the timer off.
        """
        p = PREFIX[event]
        cap = p[0].upper() + p[1:]
        state = self._current(mac)
        motion = event in ("MOTION", "VIBRATION")
        cur_on = int(state.get(f"{p}State") or 0)
        cur_rs = int(state.get(f"{p}RuntimeState") or 0)
        rs, rt = cur_rs, state.get(f"{p}Runtime") or DEFAULT_RUNTIME
        if runtime is not None:
            limit = MAX_RUNTIME.get(p, LIGHT_MAX_RUNTIME)
            if not 0 <= runtime <= limit:
                raise ValueError(f"{p} timer must be 0-{limit} minutes")
            rs, rt = (1, runtime) if runtime else (0, DEFAULT_RUNTIME)
        if not rs:
            rt = DEFAULT_RUNTIME  # the app always sends its default when the timer is off
        data: dict[str, Any] = {
            "mac": mac,
            "bassinestId": self.bassinets[mac]["id"],
            "device_id": self._client_id,
            f"{p}State": cur_on if on is None else int(on),
            f"previous{cap}State": cur_on,
            f"{p}RuntimeState": rs,
            f"{p}Runtime": rt,
            # The app sends the inverse of the new timer state for motion/vibration and
            # the tracked prior value for everything else (ControlFragment getPayloadOfControl).
            f"previous{cap}RuntimeState": (0 if rs else 1) if motion else cur_rs,
        }
        if motion:
            data[p] = _check(level, LEVELS, "level") or state.get(p) or SOOTHE_LEVEL
        elif event == "SOUND":
            data["sound"] = _check(sound, TONES, "tone") or state.get("sound") or 1
            data["volume"] = _check(volume, range(1, 4), "volume") or state.get("volume") or 1
        else:
            data[f"{p}Brightness"] = (
                _check(brightness, BRIGHTNESS, "brightness")
                or state.get(f"{p}Brightness")
                or 25
            )
        await self._send_control(event, data)

    async def _send_control(self, event: str, data: dict[str, Any]) -> None:
        """Emit, then remember what we sent until the server's echo arrives.

        Without this, a second write before the echo would rebuild its "keep current"
        fields from stale state and could undo the first (e.g. restart a stopped rocker).
        """
        await self._emit(event, data)
        self.refresh_soon()
        skip = {"mac", "bassinestId", "device_id", "eventId"}
        self._sent.setdefault(data["mac"], {}).update(
            {k: v for k, v in data.items() if k not in skip and not k.startswith("previous")}
        )

    def _current(self, mac: str) -> dict[str, Any]:
        """Server state with our not-yet-echoed writes on top, for "keep current" fields.

        Only used to build commands. Display, the re-trigger guard and the backstop read
        server-confirmed self.states, so a lost command can't look applied there.
        """
        return {**self.states.get(mac, {}), **self._sent.get(mac, {})}

    async def set_global_timer(self, mac: str, minutes: int) -> None:
        """Master timer for the whole bassinet; 0 turns it off."""
        if not 0 <= minutes <= MAX_RUNTIME["global"]:
            raise ValueError("master timer must be 0-120 minutes")
        state = self._current(mac)
        await self._send_control(
            "GLOBAL_RUNTIME",
            {
                "mac": mac,
                "device_id": self._client_id,
                "globalTimerState": 1 if minutes else 0,
                "previousGlobalTimerState": int(state.get("globalTimerState") or 0),
                "globalRuntimeTimer": minutes or state.get("globalRuntimeTimer") or DEFAULT_RUNTIME,
            },
        )

    async def set_preset(
        self,
        mac: str,
        *,
        on: bool | None = None,
        auto: bool | None = None,
        sensitivity: int | None = None,
        preset_id: str | None = None,
    ) -> None:
        """Soothing preset on/off and choice, AutoSoothe, cry sensitivity: one event."""
        state = self._current(mac)
        cur_on = int(state.get("soothingPresetState") or 0)
        cur_auto = int(state.get("soothingPresetAutoModeState") or 0)
        new_on = cur_on if on is None else int(on)
        new_auto = cur_auto if auto is None else int(auto)
        pid = str(
            preset_id or state.get("soothingPresetId") or self._default_preset.get(mac) or ""
        )
        if new_on and not pid:
            raise ValueError("choose a soothing preset first")
        if sensitivity is not None and not new_auto:
            raise ValueError("cry sensitivity applies to AutoSoothe; turn it on first")
        # Side effects mirror the app: any SOOTHING_PRESET with the preset off turns the
        # lights and master timer off; with it on, sound/rocking/vibration. The push
        # handler re-reads the snapshot once the server has applied them.
        await self._send_control(
            "SOOTHING_PRESET",
            {
                "mac": mac,
                "device_id": self._client_id,
                "soothingPresetId": pid,
                "soothingPresetState": new_on,
                "previousSoothingPresetState": cur_on,
                "soothingPresetAutoModeState": new_auto,
                "previousSoothingPresetAutoModeState": cur_auto,
                "sensitivity": _check(sensitivity, SENSITIVITY, "sensitivity")
                or state.get("sensitivity")
                or 1,
            },
        )

    async def fetch_presets(self, mac: str) -> list[str]:
        """Preset names for one bassinet (built-in and the account's own)."""
        token = await self._valid_token()
        async with self._session.get(
            f"{REST_URL}/api/soothingPresetInfo/{mac}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        names = list(
            dict.fromkeys(  # same-named presets are one preset to the cloud
                str(p["soothingPresetName"])
                for p in body.get("allPresets") or []
                if p.get("soothingPresetName")
            )
        )
        self.presets[mac] = names
        assoc = (body.get("presetAssociatedWithMac") or {}).get("soothingPresetId")
        if assoc:
            self._default_preset[mac] = str(assoc)
        return names

    async def soothe(self, mac: str, on: bool) -> None:
        """Lowest rocking + vibration on the bassinet's 2 h timer, or both off.

        Never leaves half the pair on: a failed or cancelled start sends both off. The
        HA backstop is armed before anything is sent and cleared only by a confirmed stop.
        """
        async with self._soothe_lock(mac):
            if not on:
                await self._stop(mac)
                return
            _backstop_deadlines[mac] = time.monotonic() + BACKSTOP
            self._arm_backstop(mac)
            try:
                for event in ("MOTION", "VIBRATION"):
                    await self.set_control(
                        mac, event, True, level=SOOTHE_LEVEL, runtime=SOOTHE_MINUTES
                    )
            except BaseException:
                # Finish the rollback before releasing the lock, even if cancelled again.
                rollback = asyncio.ensure_future(self._stop(mac))
                cancelled = False
                while not rollback.done():
                    try:
                        await asyncio.shield(rollback)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:  # noqa: BLE001
                        pass  # deadline stays set; the backstop retries the stop
                if cancelled:
                    raise asyncio.CancelledError from None
                raise

    def motion_running(self, mac: str) -> bool:
        """Any rocking or vibration, any level, from any source (app, AutoSoothe, HA)."""
        state = self.states.get(mac, {})
        return bool(state.get("motionState") or state.get("vibrationState"))

    def _soothe_lock(self, mac: str) -> asyncio.Lock:
        return self._soothe_locks.setdefault(mac, asyncio.Lock())

    async def _stop(self, mac: str) -> None:
        """Send both off; try both even if one fails. Clears the backstop on success.

        Caller holds the soothe lock.
        """
        errors: list[Exception] = []
        for event in ("MOTION", "VIBRATION"):
            try:
                await self.set_control(mac, event, False)
            except Exception as err:  # noqa: BLE001
                errors.append(err)
        if errors:
            raise errors[0]
        _backstop_deadlines.pop(mac, None)
        task = self._backstops.pop(mac, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def _arm_backstop(self, mac: str) -> None:
        task = self._backstops.get(mac)
        if not task or task.done():  # a live task re-reads the deadline after each sleep
            self._backstops[mac] = asyncio.create_task(self._run_backstop(mac))

    async def _run_backstop(self, mac: str) -> None:
        while (deadline := _backstop_deadlines.get(mac)) is not None:
            if (delay := deadline - time.monotonic()) > 0:
                await asyncio.sleep(delay)
                continue
            async with self._soothe_lock(mac):
                if _backstop_deadlines.get(mac, 0) > time.monotonic():
                    continue  # restarted while we waited for the lock
                state = self.states.get(mac) or {}
                if (
                    self._snapshot_seen
                    and state.get("motionState") == 0
                    and state.get("vibrationState") == 0
                ):
                    # Fresh state confirms the bassinet timer did its job. Anything less
                    # (missing keys, no snapshot yet) sends the stop: a spare off is harmless.
                    _backstop_deadlines.pop(mac, None)
                    self._backstops.pop(mac, None)
                    return
                try:
                    await self._stop(mac)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("HALO soothe backstop stop failed (%s), retrying", err)
                else:
                    _LOGGER.warning("HALO soothe backstop stopped motion the timer missed")
                    return
            await asyncio.sleep(BACKSTOP_RETRY)

    async def turn_everything_off(self, mac: str) -> None:
        await self._emit("TURN_EVERYTHING_OFF", {"turnEveryThingOff": True, "mac": mac})
        self.refresh_soon()


def _loads(value: Any) -> Any:
    """Pushes are normally objects, but accept JSON strings like the app sends.

    Returns _MALFORMED for a string that is not JSON.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        _LOGGER.warning("Ignoring malformed HALO push")
        return _MALFORMED


def _check(value: Any, allowed: Any, what: str) -> Any:
    """None passes through (keep current); anything outside the app's range is refused."""
    if value is not None and value not in allowed:
        raise ValueError(f"{what} {value!r} not in {list(allowed)}")
    return value
