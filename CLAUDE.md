# ha-halo-bassinest

Unofficial Home Assistant custom integration (`custom_components/halo_bassinest`,
domain `halo_bassinest`) for the HALO BassiNest Smart Swivel Sleeper. Talks to the
HALO cloud: REST login at `sleepsure.halosleep.com:7096`, socket.io state and
control at `:7098`. Protocol reverse-engineered from the Android app
com.halosleep.smartnursery 1.0.7, so any HALO app or server update can break it.

## State of the branches
- `main` has the client (`api.py`), setup (`__init__.py`), config flow and base
  entity. `PLATFORMS` already lists binary_sensor, button, light and switch, but
  those modules are not on `main` yet.
- Branch `worktree-platforms` adds the platform modules, `manifest.json`,
  `strings.json`, `hacs.json`, `README.md` and `tests/test_api.py`. Until it merges,
  `main` does not load in HA on its own. Another session holds a locked worktree
  for it at `.claude/worktrees/platforms`; don't edit it from here.

## Test (from `worktree-platforms`)
```
pip install aiohttp "python-socketio[asyncio_client]>=5.11"
python tests/test_api.py
```
Runs `api.py` against a local fake HALO server (login, token expiry, socket auth,
payload format, state merge). No CI and no linter configured.

## Install
Copy `custom_components/halo_bassinest` into HA `config/custom_components/`,
restart, add the integration in Settings > Devices & services.

## Hard rules
- Rocking and vibration stay read-only. Never add HA control for motion on an
  infant sleeper. Lights and sound are fine.
- No token refresh endpoint exists; the client logs in again before JWT expiry.
- Never commit HALO credentials or real bassinet MACs. Tests use a fake MAC.
