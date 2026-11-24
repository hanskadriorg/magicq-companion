#!/usr/bin/env python3
"""Deploy MagicQ Venue Companion across the LAN to venue hosts (T14 & T15).

Usage (from a machine on the 192.168.1.x venue LAN, such as H2):
    git fetch origin cursor/venue-genre-agent-d386
    git checkout cursor/venue-genre-agent-d386
    VENUE_SSH_PASS='...' python3 scripts/deploy_venue_lan.py

What it does per target (default: neli-t14@192.168.1.110, neli-t15@192.168.1.200):
  1. Fetches Essentia's offline Discogs-EffNet ONNX model once on the local machine
     so target venue PCs receive the model over SCP even if they lack internet.
  2. Preserves any existing `config.toml` on the target (`~/magicq-venue/config.toml`,
     `~/magicq-companion/config.toml`, or `~/Projects/magicq-companion/config.toml`).
  3. Cleanly stops any prior `magicq-companion` process holding port 8765 or audio.
  4. Uploads the Venue Companion bundle + `models/` into `~/magicq-venue`.
  5. Ensures `python3-venv` & `libportaudio2` are present, creates `.venv`, installs
     dependencies, and starts the unattended `magicq-venue.service` (with `@reboot`
     cron fallback when `systemd --user` is unavailable).
  6. Verifies `http://<host>:8765/api/config` and reads a live `/ws` telemetry frame.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = (
    "neli-t14@192.168.1.110",
    "neli-t15@192.168.1.200",
)

ARCHIVE_ITEMS = (
    "magicq_companion",
    "packaging",
    "scripts",
    "docs",
    "tests",
    "config.toml",
    "requirements.txt",
    "README.md",
    "Dockerfile",
    "docker-compose.yml",
)


def build_bundle_tarball(dest_tar: Path, include_models: bool = True) -> None:
    """Pack the clean Venue Companion source tree (+ models/ if present) into a .tar.gz."""
    skip_parts = {"__pycache__", ".venv", "venv", ".git", "dist", "build", "node_modules"}

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(ti.name).parts
        if any(p in skip_parts or p.endswith(".pyc") for p in parts):
            return None
        if Path(ti.name).name in ("ui_layout.json", "genre_profiles.json", "companion.log"):
            return None
        return ti

    with tarfile.open(dest_tar, "w:gz") as tf:
        for rel in ARCHIVE_ITEMS:
            src = ROOT / rel
            if src.exists():
                tf.add(src, arcname=rel, filter=_filter)
        models_dir = ROOT / "models"
        if include_models and models_dir.is_dir():
            tf.add(models_dir, arcname="models", filter=_filter)


class SshRunner:
    """Password-capable OpenSSH wrapper using temporary SSH_ASKPASS + setsid."""

    def __init__(self, password: str) -> None:
        self.password = password
        self._tmpdir = tempfile.TemporaryDirectory(prefix="mq-askpass-")
        self.askpass_path = Path(self._tmpdir.name) / "askpass.sh"
        # Read secret from environment variable inside askpass so argv/disk stay clean.
        self.askpass_path.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$MQ_INTERNAL_SSH_SECRET\"\n",
            encoding="utf-8",
        )
        self.askpass_path.chmod(stat.S_IRWXU)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MQ_INTERNAL_SSH_SECRET"] = self.password
        env["SSH_ASKPASS"] = str(self.askpass_path)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
        return env

    def ssh(
        self,
        target: str,
        remote_cmd: str,
        *,
        stdin_text: str | None = None,
        timeout: float = 180.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "setsid",
            "-w",
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=8",
            target,
            remote_cmd,
        ]
        return subprocess.run(
            cmd,
            env=self._env(),
            input=stdin_text,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def scp(self, local_path: Path, target: str, remote_path: str, *, timeout: float = 180.0) -> None:
        cmd = [
            "setsid",
            "-w",
            "scp",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=8",
            str(local_path),
            f"{target}:{remote_path}",
        ]
        subprocess.run(
            cmd,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )

    def close(self) -> None:
        self._tmpdir.cleanup()


REMOTE_SETUP_SCRIPT = r"""
set -euo pipefail
PASS_B64="$1"
PASS="$(printf '%s' "$PASS_B64" | base64 -d)"
DEST="$HOME/magicq-venue"
TAR="/tmp/magicq-venue-bundle.tar.gz"

echo "  -> Checking existing config.toml on $(hostname)..."
SAVED_CFG=""
for cand in \
  "$DEST/config.toml" \
  "$HOME/magicq-companion/config.toml" \
  "$HOME/Projects/magicq-companion/config.toml"
do
  if [ -f "$cand" ]; then
    SAVED_CFG="/tmp/magicq-venue-saved-config.toml"
    cp -f "$cand" "$SAVED_CFG"
    echo "     Preserving existing config from: $cand"
    break
  fi
done

SAVED_PROFILES=""
if [ -f "$DEST/genre_profiles.json" ]; then
  SAVED_PROFILES="/tmp/magicq-venue-saved-profiles.json"
  cp -f "$DEST/genre_profiles.json" "$SAVED_PROFILES"
  echo "     Preserving learned genre profiles from: $DEST/genre_profiles.json"
fi

echo "  -> Stopping any previous companion instance on port 8765..."
curl -fsS -m 2 -X POST http://127.0.0.1:8765/shutdown >/dev/null 2>&1 || true
if command -v systemctl >/dev/null 2>&1; then
  XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user stop magicq-venue.service >/dev/null 2>&1 || true
  XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user stop magicq-companion.service >/dev/null 2>&1 || true
fi
pkill -f "matter_sidecar/dist/index.js" >/dev/null 2>&1 || true
pkill -f "\.venv/bin/python -m magicq_companion" >/dev/null 2>&1 || true
sleep 1
rm -f /tmp/magicq-companion.lock

# Clean legacy @reboot entries so systemd handles boot while keeping scheduled reboots
if command -v crontab >/dev/null 2>&1; then
  (crontab -l 2>/dev/null | grep -v "# magicq-companion boot" || true) | crontab - 2>/dev/null || true
fi

echo "  -> Unpacking MagicQ Venue Companion into $DEST..."
mkdir -p "$DEST"
tar -xzf "$TAR" -C "$DEST"
rm -f "$TAR"
if [ -n "$SAVED_CFG" ] && [ -f "$SAVED_CFG" ]; then
  cp -f "$SAVED_CFG" "$DEST/config.toml"
  rm -f "$SAVED_CFG"
fi
if [ -n "$SAVED_PROFILES" ] && [ -f "$SAVED_PROFILES" ]; then
  cp -f "$SAVED_PROFILES" "$DEST/genre_profiles.json"
  rm -f "$SAVED_PROFILES"
fi

echo "  -> Ensuring system packages (python3-venv, libportaudio2)..."
if ! python3 -m venv --help >/dev/null 2>&1 || ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
  printf '%s\n' "$PASS" | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null
  printf '%s\n' "$PASS" | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip libportaudio2 alsa-utils libgomp1 >/dev/null
fi

echo "  -> Setting up Python venv & dependencies..."
if [ ! -x "$DEST/.venv/bin/python" ]; then
  python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

if [ ! -s "$DEST/models/discogs-effnet-bsdynamic-1.onnx" ]; then
  echo "  -> Fetching ONNX model directly on $(hostname)..."
  "$DEST/.venv/bin/python" "$DEST/scripts/fetch_models.py" --dest "$DEST/models" || true
fi

chmod +x "$DEST/packaging/start-companion.sh" "$DEST/packaging/install-venue.sh" "$DEST/packaging/install-daily-reboot.sh"

echo "  -> Configuring unattended auto-start..."
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if command -v loginctl >/dev/null 2>&1 && [ ! -f "/var/lib/systemd/linger/$(id -un)" ]; then
  printf '%s\n' "$PASS" | sudo -S -p '' loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat >"$UNIT_DIR/magicq-venue.service" <<EOF
[Unit]
Description=MagicQ Venue Companion (Offline Genre Agent & Audio Autopilot)
After=network.target sound.target pipewire.service

[Service]
Type=simple
WorkingDirectory=$DEST
ExecStart=$DEST/packaging/start-companion.sh
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u)

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable magicq-venue.service >/dev/null 2>&1 || true
  systemctl --user restart magicq-venue.service
  echo "     Started via systemd --user (magicq-venue.service)"
else
  "$DEST/packaging/install-daily-reboot.sh" || true
  nohup "$DEST/packaging/start-companion.sh" >/dev/null 2>&1 &
  echo "     Started via start-companion.sh + @reboot cron"
fi
"""


def probe_remote_dashboard(host_ip: str, port: int = 8765, timeout: float = 12.0) -> dict:
    """Wait for `/api/config` and one `/ws` frame from the deployed host."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    cfg_data: dict = {}
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host_ip}:{port}/api/config", timeout=2.5) as resp:
                cfg_data = json.loads(resp.read().decode("utf-8"))
                break
        except Exception as exc:
            last_err = exc
            time.sleep(0.6)
    if not cfg_data:
        raise RuntimeError(f"Dashboard at http://{host_ip}:{port} did not come up: {last_err}")

    ws_frame: dict = {}
    try:
        with socket.create_connection((host_ip, port), timeout=3.0) as sock:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: {host_ip}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if b"\r\n\r\n" in buf:
                _, rest = buf.split(b"\r\n\r\n", 1)
                while len(rest) < 4:
                    rest += sock.recv(4096)
                length = rest[1] & 0x7F
                offset = 2
                if length == 126:
                    length = struct.unpack("!H", rest[2:4])[0]
                    offset = 4
                elif length == 127:
                    length = struct.unpack("!Q", rest[2:10])[0]
                    offset = 10
                while len(rest) < offset + length:
                    rest += sock.recv(4096)
                ws_frame = json.loads(rest[offset : offset + length].decode("utf-8"))
    except Exception:
        pass

    return {"config": cfg_data, "ws": ws_frame}


def deploy_target(runner: SshRunner, target: str, tarball: Path, password: str) -> dict:
    host_ip = target.split("@", 1)[-1]
    print(f"\n=== Deploying to {target} ===")
    runner.scp(tarball, target, "/tmp/magicq-venue-bundle.tar.gz")
    pass_b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
    res = runner.ssh(
        target,
        f"bash -s -- '{pass_b64}'",
        stdin_text=REMOTE_SETUP_SCRIPT,
        timeout=300.0,
        check=False,
    )
    if res.stdout.strip():
        print(res.stdout.rstrip())
    if res.returncode != 0:
        raise RuntimeError(
            f"Remote setup failed on {target} (exit {res.returncode}):\n{res.stderr.strip()}"
        )
    status = probe_remote_dashboard(host_ip)
    meta = status["config"].get("meta", {})
    ws = status["ws"]
    print(f"  -> LIVE http://{host_ip}:8765")
    print(f"     Source      : {meta.get('source')}")
    print(f"     DMX Output  : {meta.get('artnet')}")
    print(f"     Genre Agent : mode={meta.get('genre_mode')} backend={ws.get('genre_backend') or meta.get('genre_backend')}")
    if ws:
        print(
            f"     Telemetry   : section={ws.get('section')} bpm={ws.get('bpm')} "
            f"pace={ws.get('pace')} divider={ws.get('bpm_rate')} fx_size={ws.get('fx_size')} "
            f"archetype={ws.get('archetype_label')} ({ws.get('rms_db')} dBFS)"
        )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy MagicQ Venue Companion to T14 and T15 over LAN.")
    parser.add_argument(
        "targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="SSH targets (default: neli-t14@192.168.1.110 neli-t15@192.168.1.200)",
    )
    parser.add_argument(
        "--skip-local-model-fetch",
        action="store_true",
        help="Skip pre-fetching the ONNX model locally before uploading.",
    )
    args = parser.parse_args(argv)

    password = os.environ.get("VENUE_SSH_PASS") or ""
    if not password:
        password = getpass.getpass("SSH/sudo password for venue hosts: ")

    if not args.skip_local_model_fetch:
        print("==> Ensuring offline Discogs-EffNet ONNX model is cached locally...")
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "fetch_models.py"), "--dest", str(ROOT / "models")],
            check=False,
        )

    with tempfile.TemporaryDirectory(prefix="mq-venue-bundle-") as tmp:
        tarball = Path(tmp) / "magicq-venue-bundle.tar.gz"
        print("==> Building clean deployment bundle...")
        build_bundle_tarball(tarball, include_models=True)
        print(f"    Bundle size: {tarball.stat().st_size / (1024 * 1024):.2f} MiB")

        runner = SshRunner(password)
        failures = 0
        try:
            for target in args.targets:
                try:
                    deploy_target(runner, target, tarball, password)
                except Exception as exc:
                    failures += 1
                    print(f"ERROR on {target}: {exc}", file=sys.stderr)
        finally:
            runner.close()

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
