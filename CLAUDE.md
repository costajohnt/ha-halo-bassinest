# ha-halo-bassinest

Unofficial Home Assistant custom integration (`custom_components/halo_bassinest`,
domain `halo_bassinest`) for the HALO BassiNest Smart Swivel Sleeper. Talks to the
HALO cloud: REST login at `sleepsure.halosleep.com:7096`, socket.io state and
control at `:7098`. Protocol reverse-engineered from the Android app
com.halosleep.smartnursery 1.0.7, so any HALO app or server update can break it.

## Layout
- `api.py` (client), `__init__.py` (setup), `config_flow.py` (user + reauth),
  `entity.py` (base), and the binary_sensor, button, light and switch platforms.
  `manifest.json` pins `python-socketio[asyncio_client]==5.17.1`; `hacs.json` needs
  HA 2024.11+ (reauth uses `_get_reauth_entry`).

## Test
```
pip install aiohttp "python-socketio[asyncio_client]==5.17.1" pytest ruff
ruff check custom_components tests
pytest -q
```
`tests/test_client.py` runs `api.py` against a local fake HALO cloud behind a TCP
proxy (`tests/conftest.py`): error classification, state clearing on disconnect,
reconnect after drops and server kicks, backoff, reauth stop, malformed pushes;
`tests/test_reconnect_edge_cases.py` covers unload mid-reconnect, kick loops and
lost snapshots.
`tests/test_api.py` checks the wire format (also runnable as
`python tests/test_api.py`). `.github/workflows/ci.yml` runs the same. Never point
tests at the real cloud or a real bassinet.

## Install
Copy `custom_components/halo_bassinest` into HA `config/custom_components/`,
restart, add the integration in Settings > Devices & services.

## Hard rules
- Motion control is limited to the "Soothe 2h (low)" switch: lowest rocking and
  vibration level on the bassinet's own 2 h timer (owner decision 2026-11-13).
  Disabled by default, HA backstop stop at 2 h 5 min, no re-trigger while on.
  No level or duration knobs, never drive it from automations. Lights and sound are fine.
- No token refresh endpoint exists; the client logs in again before JWT expiry.
- Never commit HALO credentials or real bassinet MACs. Tests use a fake MAC.
