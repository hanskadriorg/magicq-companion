# AGENTS.md

## Cursor Cloud specific instructions

MagicQ Companion is a single product with two cooperating runtimes:

- Python app (`magicq_companion/`): live audio analysis + Art-Net/sACN DMX output + an aiohttp web dashboard on port `8765`.
- Node.js Matter sidecar (`matter_sidecar/`): virtual Matter lights (Fastify API on `127.0.0.1:3081`) that report levels back to the Python app.

There is no automated test suite and no linter configured. Simulation mode is the built-in end-to-end mechanism (see `README.md` "Run" and `magicq_companion/simulate.py`).

### Running the app

Use the Python venv created by the update script (`.venv`). Standard run commands are documented in `README.md` "Run"; the ones useful in a headless cloud VM (no audio hardware, no real MagicQ) are:

- Headless pipeline check (no DMX, exits with `--duration`): `.venv/bin/python -m magicq_companion --simulate --dry-run --fast --duration 8`
- Full app + dashboard: `.venv/bin/python -m magicq_companion --simulate --ui` then open `http://localhost:8765`.

Always pass `--simulate` in the cloud VM: there is no audio input device, so live-capture modes (`sounddevice`/`pw-record`/`arecord`) have nothing to read.

### Non-obvious gotchas

- The Matter sidecar is auto-started as a subprocess by the Python app only when `--ui` is used; console-only mode does not start it. On first `--ui` start the app runs `npm install`/`npm run build` in `matter_sidecar/` if `dist/` is missing or stale (see `magicq_companion/matter_runtime.py`). Pre-installing node_modules (the update script does) avoids that first-run delay.
- The Python process is line/block-buffered when its stdout is piped (e.g. `| tee`); a piped log can look empty even though the server is up. Verify with `curl -s -o /dev/null -w '%{http_code}' http://localhost:8765/` instead.
- `ss -tlnp` may not show the listener without privileges; a `curl` to port `8765` is the reliable readiness check.
- There is no `/api/state` route. Real endpoints include `/` (dashboard), `/ws` (WebSocket live state), `/api/config`, `/devices`, `/api/layout`, and `/api/matter/{path}` (proxied to the sidecar, e.g. `/api/matter/devices`).
- Default `config.toml` targets Art-Net broadcast to `255.255.255.255:6454` and a MagicQ at `2.0.0.15`; no receiver is required for the app to run (nothing errors if DMX packets go unheard). Use `--dry-run` to suppress sending entirely.
- System packages `libportaudio2`, `alsa-utils`, and `python3.12-venv` are required and are baked into the VM image (not reinstalled by the update script).
