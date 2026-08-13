# MagicQ Companion

Club lighting companion for ChamSys MagicQ: live audio analysis, Matter
virtual lights, and a single Art-Net/sACN stack you can extend later.

This is **v2.0.0**, renamed from `magicq-audio-matter-bridge`. It grew out of
[magicq-audio-bridge](https://github.com/hanskadriorg/magicq-audio-bridge)
and [matter-artnet-bridge](https://github.com/hanskadriorg/matter-artnet-bridge).

Audio analysis and Matter lights both send **real Art-Net/sACN** to MagicQ
on the same NIC, with a separate protocol/universe per segment.

```text
[DJ mixer] --> [audio analysis] --Art-Net/sACN universe A--> [MagicQ]
[phone / Home] --> [Matter nodes] --Art-Net/sACN universe B--> [MagicQ]
                              \ same NIC /
```

Network output supports **Art-Net** (unicast or broadcast) or **sACN
(E1.31)** (unicast or multicast) per segment. Pick the NIC once in Settings;
configure `[network]`, `[artnet]` (audio), and `[matter]` in `config.toml`.

## What audio sends

One Art-Net universe (default: universe 0) with these channels:

| DMX ch | Name           | Behaviour                                                    |
|--------|----------------|--------------------------------------------------------------|
| 1      | groove         | 255 while the steady main section of the track is playing    |
| 2      | breakdown      | 255 while the kick is gone (atmospheric section)             |
| 3      | buildup        | 255 while tension is rising toward a drop                    |
| 4      | drop           | 255 for ~16 s after the drop hits                            |
| 5      | build_progress | Ramps 0 to 255 during a build-up, slams to 255 on the drop   |
| 6      | beat_pulse     | Toggles 0↔255 on every kick (hard edges for MagicQ BPM)      |
| 7      | energy         | Loudness vs. the recent average (128 = normal level)         |
| 8      | kick           | Bass-band envelope follower (like a smarter 63 Hz trigger)   |
| 9      | drop_hit       | 255 for ~150 ms only at the instant the drop starts          |
| 10     | melody         | 0–255 when kick continues but mid/high (pads/leads) dominate |
| 11     | four_four      | 255 pulse on each 4/4 quarter note (grid-locked chase sync)  |
| 12     | centroid       | Spectral centroid — dark/bass-heavy → bright/trebly          |
| 13     | tilt           | (mid+high)/bass — how open the mix is vs the kick            |
| 14     | bpm_rate       | 255/128/64/32 = full/½/¼/⅛ BPM chase rate (by energy)       |

Exactly one of channels 1-4 is at 255 at any time. Channel 9 is a
one-shot impact trigger (strobe / cue / macro); channel 4 holds the
drop look for the full hold time. Channel 10 rises when the track gets
more melodic without dropping the kick. Channel 11 is a phase-locked
4/4 metronome (steadier than channel 6’s raw kick pulse). Channels
12–13 describe brightness/texture; channel 14 picks a tempo divisor
from energy (range + decay hold are set in the UI / `[bpm_rate]` in
`config.toml`) and crossfades in 0.25 s. Channel numbers are
configurable in `config.toml`.

The dashboard also has **analyzer on/off**, **output on/off** (audio Art-Net
blackout), Matter device pairing QR codes, a **build/drop intensity** slider,
**drop length**, editable **grid layout**, and a **reboot** button for headless
Raspberry Pi installs.

## Matter devices

Create On/Off or On/Off+RGB Matter nodes in the dashboard, then scan the QR
in Apple Home / Google Home / etc. Channel mapping:

| Device type | Matter clusters | DMX channels |
|-------------|-----------------|--------------|
| On / Off | OnOff | 1 channel: `0` off / `255` on |
| On / Off + RGB | OnOff + LevelControl + ColorControl | Intensity, R, G, B |

All Matter fixtures share the **Matter → MagicQ** protocol/universe from
Settings. Pairing identities live in `data/matter/`. Matter mDNS is bound to
the same NIC as DMX.

Requires **Node.js 20+** (in addition to Python) so the matter.js sidecar can
run. The first `--ui` start runs `npm install` and `npm run build` in
`matter_sidecar/`.

## Setup

Requires Python 3.11+, Node.js 20+, and PortAudio (Linux: `sudo apt install libportaudio2`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Edit `config.toml`:

- `[network] ip` — MagicQ unicast address (when a segment is unicast)
- `[network] interface` — NIC for both audio and Matter DMX + Matter mDNS
- `[artnet]` — audio universe / protocol / mode
- `[matter]` — Matter universe / protocol / mode
- `[audio] device` — substring of the input device name
  (list devices with `python -m magicq_companion --list-devices`)

Feed the program a clean audio signal: the DJ mixer's booth/record output
into an audio interface input is ideal. A room microphone works but is
noisier and will make detection less reliable.

## Run

```bash
# Live audio, sending Art-Net as configured:
.venv/bin/python -m magicq_companion

# Same, with the web dashboard at http://localhost:8765
.venv/bin/python -m magicq_companion --ui

# Test the whole pipeline with a built-in synthetic techno track:
.venv/bin/python -m magicq_companion --simulate

# Analyze only, no Art-Net (safe to try anywhere):
.venv/bin/python -m magicq_companion --simulate --dry-run --fast
```

The console shows the detected section, BPM, and live meters. On exit the
program sends a blackout frame on its universe so no input channels stay up.

## Windows exe / MSI

Yes — you can ship a self-contained Windows build with Python and all
dependencies (numpy, sounddevice/PortAudio, aiohttp) baked in. PyInstaller
**must be run on Windows** (cross-compiling from Linux is not reliable).

### Portable zip (recommended)

On a Windows machine, from the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Output:

- `dist\MagicQCompanion\MagicQCompanion.exe` — double-click starts the UI
- `dist\MagicQCompanion-windows.zip` — copy this folder anywhere

Settings are stored in `%APPDATA%\magicq-companion\config.toml` so the
install location can stay read-only.

### MSI installer

The GitHub Action (below) builds an MSI with WiX after the PyInstaller
step. Locally (Windows, with [WiX](https://wixtoolset.org/) installed):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
wix build packaging\Product.wxs -d PayloadDir=$PWD\dist\MagicQCompanion -o dist\MagicQCompanion.msi
```

### Setup.exe (Inno Setup, optional)

If you prefer a classic wizard installer instead of MSI:

1. Build with `packaging\build_windows.ps1`
2. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php)
3. Compile `packaging\installer.iss` → `dist\MagicQCompanion-Setup.exe`

### GitHub Actions

Push a tag (`v0.1.0`) or run **Build Windows** from the Actions tab. It
uploads:

- `MagicQCompanion-windows.zip` (portable)
- `MagicQCompanion.msi` (installer)

Windows Defender may warn on unsigned builds the first time — that is
normal for unsigned open-source exes; use “More info → Run anyway”, or
code-sign the binary for club/production machines.

## Docker

For deployment on a dedicated machine:

```bash
docker compose up -d --build
```

This runs the bridge with the web dashboard at `http://<host>:8765`.
The compose file uses:

- `network_mode: host` — so Art-Net UDP (including broadcast) reaches the
  lighting network directly and the dashboard port is exposed as-is
- `devices: /dev/snd` — passes the sound card (e.g. the Scarlett) into
  the container; audio is captured via ALSA inside the container
- a read-only mount of `config.toml` — edit it on the host and
  `docker compose restart` to apply

Note: on a desktop host where PipeWire/PulseAudio owns the sound card,
the container's direct ALSA access can conflict with the desktop using
the same device. On a dedicated/headless box (the intended deployment)
this is not an issue. Device names inside the container are ALSA names —
`device = "Scarlett"` still matches.

To watch logs or the console output: `docker compose logs -f`.

## Web dashboard

`--ui` serves a live dashboard (default port 8765, change with
`--ui-port`). It shows the current section, BPM with a beat indicator,
energy/kick/build/ramp meters, a 60-second energy timeline colored by
detected section, the outgoing DMX channel values, and a log of section
transitions. It is reachable from other machines on the network too
(e.g. a tablet at FOH: `http://<pc-ip>:8765`).

The **Settings** panel at the top lets you pick the audio input device,
Art-Net target IP, and universe while the bridge is running. Apply writes
the values back to `config.toml`. Changing the input device restarts
capture; changing Art-Net swaps the sender without interrupting analysis.

## MagicQ setup

1. **Setup → View DMX I/O**: on a spare universe row set **Input** to
   `Art-Net` and **In Uni** to the universe from `config.toml` (default 0).
2. Map the input channels to playbacks. Two common approaches:
   - **Setup → View Settings → Playback**: set *Playbacks DMX trigger* so
     DMX input channels control playback faders PB1-PB10 directly. Then
     channel 1 = PB1 (groove look), channel 2 = PB2 (breakdown look),
     channel 3 = PB3 (build look), channel 4 = PB4 (drop look),
     channel 5 = PB5 (a fader you program as "build intensity").
   - Or use **automations** (Macro window) triggered on DMX input levels
     for more complex actions (page changes, macros, etc.).
3. Program the four looks as playbacks. Suggested feel:
   - **Groove**: beat-synced chases, medium intensity
   - **Breakdown**: slow airy colors, no beat chases
   - **Build-up**: accelerating movement; let PB5 (build_progress) drive
     an intensity/speed master so the ramp is music-timed
   - **Drop**: full send - strobes, blinders, open white
4. Keep a manual override playback above the automated ones so a human can
   always take over.

Note: on MagicQ **PC/Mac**, DMX-input control of playbacks requires an
unlocked system (a ChamSys USB interface/wing connected, or network
universes unicast to a GeNetix/SnakeSys node). MagicQ consoles support it
natively. See the ChamSys manual, "PB1 to PB10 controlled by DMX input".

## Tuning

All detection thresholds live in `config.toml` with comments. The ones you
are most likely to touch:

- `drop_hold_seconds` — how long the drop look stays before returning to
  groove
- `evidence_seconds` / `min_dwell_seconds` — higher = more stable, slower
  to react; lower = snappier, more prone to flicker
- `[build_score]` weights — how much rising loudness vs. rising hi-hats
  vs. onset density count toward "this is a build-up"

Run with `--dry-run` in the booth during a real set and watch the console
output before connecting it to the rig.

## How it works

- `features.py` — FFT per 12 ms hop: kick-band and treble-band energy,
  spectral flux onsets, BPM from kick intervals, and 8-second trend slopes,
  all normalized against slow rolling baselines so it adapts to gain changes.
- `state_machine.py` — hysteresis state machine over those features;
  a drop requires the bass to slam back after silence, or a sudden
  loudness jump during a build.
- `artnet.py` — minimal ArtDMX sender (no dependencies).
- `simulate.py` — synthetic techno track (128 BPM, scripted sections) for
  end-to-end testing without audio hardware.
