#!/bin/bash
# Allow the MagicQ Companion web dashboard (default TCP 8765) through ufw.
# Safe to run repeatedly. Does nothing if ufw is not installed or inactive.
set -euo pipefail

PORT="${1:-8765}"

if ! command -v ufw >/dev/null 2>&1; then
  echo "ufw not installed; if another firewall blocks port ${PORT}, allow it manually."
  exit 0
fi

if ! sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "ufw is not active; no rule added."
  echo "If other devices still cannot reach http://<pi-ip>:${PORT}, check iptables or your router."
  exit 0
fi

sudo ufw allow "${PORT}/tcp" comment "MagicQ Companion dashboard"
echo "Allowed TCP ${PORT} (MagicQ Companion dashboard)."
sudo ufw status numbered | grep -E "${PORT}|MagicQ" || true
