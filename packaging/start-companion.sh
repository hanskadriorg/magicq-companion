#!/bin/bash
# Start MagicQ Companion after boot (used by @reboot cron).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/companion.log"
LOCK="/tmp/magicq-companion.lock"
export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"
export PYTHONUNBUFFERED=1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="$ROOT/.node/bin:$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

for _ in $(seq 1 45); do
  if [ -S "${XDG_RUNTIME_DIR}/pipewire-0" ] || [ -d /dev/snd ]; then
    break
  fi
  sleep 2
done

if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

cd "$ROOT"
exec /usr/bin/flock -n "$LOCK" \
  "$PY" -m magicq_companion --ui --config "$ROOT/config.toml" \
  >>"$LOG" 2>&1
