# HALO BassiNest for Home Assistant

Custom integration for the HALO BassiNest Smart Swivel Sleeper (the WiFi model that uses the "HALO BassiNest" / Halo Smart Nursery app). Unofficial. The protocol was worked out from the Android app (com.halosleep.smartnursery 1.0.7), so HALO can break it with any app or server update.

## What you get (per bassinet)

| Entity | Type | Notes |
|---|---|---|
| Online | binary_sensor | bassinet connected to the HALO cloud |
| Rocking, Vibration | binary_sensor | read-only on purpose (see Safety) |
| AutoSoothe | binary_sensor | cry-response mode enabled |
| Night light, Floor light, Back light | light | brightness snaps to 25/50/75/100 |
| Sound | switch | on/off with the tone and volume last set in the app; tone/volume as attributes |
| Turn everything off | button | same as the app's button |

Cry events are not exposed: the app only gets them as push notifications, not over the API.

## How it talks to HALO

- `POST https://sleepsure.halosleep.com:7096/api/auth/login` for a JWT. There is no refresh endpoint, so the integration logs in again shortly before the token expires.
- socket.io at `https://sleepsure.halosleep.com:7098`. Auth `{"token": <jwt>}`, control events carry a JSON *string* payload, state is pushed (`iot_class: cloud_push`).
- HA needs outbound access to ports 7096 and 7098.

## Install

HACS: HACS > Integrations > three-dot menu > Custom repositories, add `https://github.com/costajohnt/ha-halo-bassinest` as an Integration, install HALO BassiNest, restart HA.

Manual: copy `custom_components/halo_bassinest` into your HA `config/custom_components/`, restart, then Settings > Devices & services > Add integration > HALO BassiNest.

Tip: invite a second user to the bassinet in the HALO app and give HA that login, in case the backend allows only one session per user.

## Safety

Rocking and vibration are not controllable from HA. An automation glitch should not be able to start motion on an infant sleeper. Lights and sound are fair game.

## Test

```
pip install aiohttp "python-socketio[asyncio_client]==5.17.1" pytest
pytest -q
```

Runs the client against a local fake HALO server (login, token expiry, socket auth, payload format, state merge).

## License

MIT. Not affiliated with or endorsed by HALO Innovations.
