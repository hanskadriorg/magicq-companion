#!/bin/bash
# One-command unattended setup for venue hosts (e.g. ThinkPad T14 and T15).
# Creates .venv, installs Python + ONNX dependencies, fetches offline genre
# model weights, and registers a self-healing systemd user service (plus cron fallback).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> [1/4] Preparing Python virtual environment..."
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"

echo "==> [2/4] Fetching offline Discogs-EffNet ONNX genre model..."
"$ROOT/.venv/bin/python" "$ROOT/scripts/fetch_models.py" --dest "$ROOT/models"

chmod +x "$ROOT/packaging/start-companion.sh" "$ROOT/packaging/install-daily-reboot.sh"

echo "==> [3/4] Configuring systemd user service for unattended auto-restart..."
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat >"$UNIT_DIR/magicq-venue.service" <<EOF
[Unit]
Description=MagicQ Venue Companion (Offline Genre Agent & Audio Autopilot)
After=network.target sound.target pipewire.service

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m magicq_companion --ui --config $ROOT/config.toml
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now magicq-venue.service
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$(id -un)" 2>/dev/null || true
  fi
  echo "    systemd service active: systemctl --user status magicq-venue.service"
else
  echo "    systemd --user unavailable; installing @reboot cron entry..."
  "$ROOT/packaging/install-daily-reboot.sh"
fi

echo "==> [4/4] Done. Dashboard available at http://localhost:8765"
