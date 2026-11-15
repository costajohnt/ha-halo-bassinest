# HALO BassiNest for Home Assistant

Custom integration for the HALO BassiNest Smart Swivel Sleeper (the WiFi model that uses the "HALO BassiNest" / Halo Smart Nursery app). Unofficial. The protocol was worked out from the Android app (com.halosleep.smartnursery 1.0.7), so HALO can break it with any app or server update.

## What you get (per bassinet)

| Entity | Type | Notes |
|---|---|---|
| Online | binary_sensor | bassinet connected to the HALO cloud |
| Rocking, Vibration | binary_sensor | status |
| Soothe 2h (low) | switch | lowest rocking + vibration, the bassinet's own 2 h timer ends it (see Safety) |
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

The only motion control is the Soothe 2h (low) switch: lowest rocking and vibration level, with the 2 hour stop handed to the bassinet's own timer so it ends even if HA is down. There are no level or duration knobs. Don't drive it from automations. Lights and sound are fair game.

## Limitations

- Cloud only. If HALO's servers or the app protocol change, the integration can break without warning.
- Bassinets are discovered at setup. Reload the integration after adding one to your HALO account.
- Light brightness has four steps (25/50/75/100).
- Cry events are not available (push notifications only).

## Troubleshooting

Turn on debug logging in `configuration.yaml`, restart, and reproduce the problem:

```yaml
logger:
  logs:
    custom_components.halo_bassinest: debug
```

Remove email addresses and MAC addresses before posting logs in an issue.

## Removal

Settings > Devices & services > HALO BassiNest > three-dot menu > Delete, then remove it in HACS (or delete `custom_components/halo_bassinest`) and restart.

## Test

```
pip install aiohttp "python-socketio[asyncio_client]==5.17.1" pytest
pytest -q
```

Runs the client against a local fake HALO server (login, token expiry, socket auth, payload format, state merge).

## License

MIT. Not affiliated with or endorsed by HALO Innovations.
