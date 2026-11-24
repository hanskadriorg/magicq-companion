#!/bin/bash
# Export this checkout into a standalone repository (default: ../magicq-venue).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$(dirname "$SRC")/magicq-venue}"

if [ -e "$DEST" ]; then
  echo "Destination already exists: $DEST" >&2
  exit 1
fi

mkdir -p "$DEST"
git -C "$SRC" archive HEAD | tar -x -C "$DEST"
git -C "$DEST" init -b main
git -C "$DEST" add -A
git -C "$DEST" commit -m "Initialize MagicQ Venue Companion"

echo "Standalone venue repository created at: $DEST"
