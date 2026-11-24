# MagicQ Venue Companion

Unattended venue lighting autopilot for ChamSys MagicQ with an **offline ONNX music-genre agent** and **compression-invariant pace & section DSP**.

Designed for dedicated venue computers (such as **T14** and **T15**) running MagicQ unattended across shifting nights, DJs, and genres — without personal-rig extras (Standalone FX, Matter-to-DMX, Node.js sidecar, and Pixel Mapper are stripped).

```text
[DJ mixer] --> [Audio + Watchdog] --> [Offline ONNX Genre Agent (400 genres -> 8 archetypes)]
                                  \-> [True BPM (60-190) + Intrinsic Pace DSP]
                                           \-- Art-Net / sACN (Ch 1-9) --> [MagicQ]
```

## Key capabilities

- **Universal Offline Genre Agent (`magicq_companion/genre_agent.py`)**:
  - Runs Essentia's **Discogs-EffNet** (`discogs-effnet-bsdynamic-1.onnx`, 400 genres/subgenres) locally on CPU every 3 seconds over an 8-second 16 kHz Slaney log-mel buffer — zero internet dependency in the venue, with a built-in harmonic/percussive + tempogram DSP fallback if model weights are absent.
  - Smoothly blends probabilities across **8 Musical Archetypes** (`ambient_chill`, `groovy_warm`, `house_tech`, `melodic_prog`, `club_peak`, `hiphop_urban`, `fast_bass`, `rock_live`) over a 12-second EMA so neither stage PC is locked to a single style.
- **True Tempo (`60–190 BPM`) & Intrinsic Pace Score (`magicq_companion/features.py`)**:
  - Replaces the old `90–180 BPM` octave clamp so slow `70–89 BPM` R&B, Hip-Hop, Reggaeton, and Downtempo tracks stay at their true tempo instead of doubling to `140–178 BPM`.
  - Replaces relative-RMS quartile stretching with a compression-invariant **Intrinsic Pace Score** (transient density, 2–8 kHz hi-hat subdivision rate, kick punch, true tempo, and genre arousal) so quiet/slow songs stay on `1/8`–`1/4` chases (`32`/`64`) while brickwall-mastered peak club tracks stay energized on `1/2`–`1/1` (`128`/`255`) — and `174 BPM` Drum & Bass grooves cap at `128` (`1/2`, ~87 BPM feel) while unlocking `255` on peak drops.
- **Syncopated Groove Stability (`magicq_companion/state_machine.py`)**:
  - Adapts breakdown detection to non-4/4 drum patterns (`hiphop_urban`, `fast_bass`, `rock_live`) so 2-step, breakbeat, and trap grooves do not falsely flicker into `BREAKDOWN`.
- **Unattended Audio Watchdog (`magicq_companion/pipeline.py`)**:
  - Automatically recovers and reconnects if PipeWire restarts or a USB audio interface briefly drops out mid-event.

## Outgoing DMX channels (unchanged MagicQ contract)

| DMX ch | Name | Behaviour |
|--------|------|-----------|
| 1 | `groove` | `255` while the main section is playing |
| 2 | `breakdown` | `255` while the kick/beat drops out |
| 3 | `buildup` | `255` while tension is rising toward a drop |
| 4 | `drop` | `255` for the drop hold duration |
| 5 | `build_progress` | Ramps `0` to `255` during a build-up, `255` on drop |
| 6 | `beat_pulse` | Hard `0`/`255` toggle on every detected beat |
| 7 | `drop_hit` | One-shot `255` flash at drop start |
| 8 | `four_four` | `255` while a steady 4/4 kick grid is locked |
| 9 | `bpm_rate` | `32` / `64` / `128` / `255` (`1/8` / `1/4` / `1/2` / `1/1`) from Genre Agent + Intrinsic Pace |
| 10 | `drop_momentary` | Held at `0` in unattended autopilot |

Existing `config.toml` files from `magicq-companion` (including legacy channel addresses or `[app]`, `[fx]`, and `[matter]` sections) load cleanly without modification.

## Setup on T14 & T15 (Linux)

### Automated LAN deploy from a laptop on the venue network

```bash
git fetch origin cursor/venue-genre-agent-d386
git checkout cursor/venue-genre-agent-d386
python3 scripts/deploy_venue_lan.py neli-t14@192.168.1.110 neli-t15@192.168.1.200
```

This pre-fetches the offline Discogs-EffNet ONNX model locally once, preserves each machine's existing `config.toml` (audio device, noise floor, and MagicQ network/universe), stops any previous `magicq-companion` instance cleanly, installs `.venv` + `onnxruntime`, enables `magicq-venue.service` (`Restart=always`), and verifies live HTTP + WebSocket telemetry on both `http://192.168.1.110:8765` and `http://192.168.1.200:8765`.

### Direct install on a single venue PC

Requires Python 3.11+ and PortAudio / PipeWire (`sudo apt install libportaudio2`). No Node.js required.

```bash
./packaging/install-venue.sh
```

This creates `.venv`, installs `numpy`, `sounddevice`, `aiohttp`, and `onnxruntime`, downloads `models/discogs-effnet-bsdynamic-1.onnx` + `.json` once for offline operation, and enables the `magicq-venue.service` user unit (`Restart=always`).

## Manual / CLI usage

```bash
# Fetch offline ONNX genre model once:
.venv/bin/python scripts/fetch_models.py

# Run with the live web dashboard at http://localhost:8765:
.venv/bin/python -m magicq_companion --ui

# Test end-to-end with the built-in multi-genre venue showcase (Deep House -> Hip-Hop -> Techno -> DnB):
.venv/bin/python -m magicq_companion --simulate --ui

# Fast headless verification without sending DMX:
.venv/bin/python -m magicq_companion --simulate --dry-run --fast

# Export as a standalone repository folder (e.g. ../magicq-venue):
./scripts/export_venue_repo.sh ../magicq-venue
```
