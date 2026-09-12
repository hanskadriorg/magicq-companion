#!/bin/bash
# Apply @reboot companion start and the [server] reboot schedule from config.toml.
# Scheduled reboot stays off unless [server] reboot_enabled is true.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$ROOT/packaging/start-companion.sh"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi
cd "$ROOT"
exec "$PY" -m magicq_companion.schedule --config "$ROOT/config.toml" --root "$ROOT"
