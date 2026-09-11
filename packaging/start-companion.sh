#!/bin/bash
# Start MagicQ Companion after boot (used by @reboot cron).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/companion.log"
LOCK="/tmp/magicq-companion.lock"
export HOME="${HOME:-/home/oskar}"
export PYTHONUNBUFFERED=1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

for _ in $(seq 1 45); do
  if [ -S "${XDG_RUNTIME_DIR}/pipewire-0" ] || [ -d /dev/snd ]; then
    break
  fi
  sleep 2
done

cd "$ROOT"
exec /usr/bin/flock -n "$LOCK" \
  "$ROOT/.venv/bin/python" -m magicq_companion --ui --config "$ROOT/config.toml" \
  >>"$LOG" 2>&1
