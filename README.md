# HALO BassiNest for Home Assistant

Custom integration for the HALO BassiNest Smart Swivel Sleeper (the WiFi model that uses the "HALO BassiNest" / Halo Smart Nursery app). Unofficial. The protocol was worked out from the Android app (com.halosleep.smartnursery 1.0.7), so HALO can break it with any app or server update.

## What you get (per bassinet)

Everything the HALO app can control:

| Entity | Type | Notes |
|---|---|---|
| Rocking, Vibration | switch | on/off with the level and timer currently set |
| Rocking level, Vibration level | select | Low / High |
| Rocking timer, Vibration timer | number | 1-120 minutes, 0 = no timer |
| Sound | switch | on/off |
| Sound tone | select | Brown Noise, Pink Noise, White Noise, Heart Beat, Stream |
| Volume | number | 1-3 |
| Sound timer | number | 1-600 minutes, 0 = no timer |
| Night light, Floor light, Back light | light | brightness 25/50/75/100% |
| Night/Floor/Back light timer | number | 1-600 minutes, 0 = no timer |
| Master timer | number | 1-120 minutes for the whole bassinet, 0 = off. In a live test, setting it stopped rocking within seconds instead of counting down; behaviour not fully understood |
| AutoSoothe | switch | respond to crying with the selected preset |
| Cry sensitivity | number | 1-4, while AutoSoothe is on (as in the app) |
| Preset | select | the account's soothing presets (built-in and your own) |
| Soothing preset | switch | run the selected preset now |
| Soothe 2h (low) | switch | one tap: low rocking + vibration on the bassinet's 2 h timer (see Safety) |
| Turn everything off | button | same as the app's button |
| Online | binary_sensor | bassinet connected to the HALO cloud |

Levels, timers and cry sensitivity are configuration entities, so they sit under the device's Configuration section and stay out of the default dashboard. Creating or editing presets stays in the app.

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

This integration can start rocking and vibration on an infant sleeper, the same as the app. Use the bassinet's own timers (set on the bassinet, so they end even if Home Assistant or your network goes down), and think twice before triggering motion from automations or voice assistants.

Rocking and Vibration are ordinary switches, so voice assistants and area-wide commands ("turn on everything in the nursery") can reach them. Consider unexposing them under Settings > Voice assistants.

Changing a soothing preset, AutoSoothe or cry sensitivity sends the app's single preset command, which has the app's side effects: with the preset off it also turns the lights and master timer off; with it on, sound, rocking and vibration follow the preset.

Soothe 2h (low) is the cautious one-tap option: lowest rocking and vibration on the bassinet's 2 hour timer, plus a stop from Home Assistant 5 minutes after that in case the timer didn't fire. Turning it on while any motion is running does nothing, so it can't extend a session. Changing the rocking or vibration timer during a Soothe session replaces the bassinet's 2 hour stop; the Home Assistant stop still applies unless Home Assistant restarts.

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

Remove email addresses and MAC addresses before posting logs in an issue. For a state dump, use Download diagnostics on the integration's menu: emails, MACs, ids and names are redacted.

## Removal

Settings > Devices & services > HALO BassiNest > three-dot menu > Delete, then remove it in HACS (or delete `custom_components/halo_bassinest`) and restart.

## Test

```
pip install aiohttp "python-socketio[asyncio_client]==5.17.1" pytest
pytest -q
```

Home Assistant layer tests (config flow, setup, entities, diagnostics) need `pip install pytest-homeassistant-custom-component==0.13.316` and run with `pytest -q tests/ha`.

The client tests run against a local fake HALO server (login, token expiry, socket auth, payload format, state merge).

## License

MIT. Not affiliated with or endorsed by HALO Innovations.
