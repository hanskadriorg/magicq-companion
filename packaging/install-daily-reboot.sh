#!/bin/bash
# Install a daily 14:00 local-time host reboot.
# Prefers root crontab; falls back to user crontab with passwordless sudo reboot.
set -euo pipefail

MARKER="# magicq-companion daily reboot"
ENTRY="0 14 * * * /sbin/reboot ${MARKER}"

install_root() {
  TMP=$(mktemp)
  sudo crontab -l 2>/dev/null | grep -v 'magicq-companion daily reboot' >"$TMP" || true
  printf '%s\n' "$ENTRY" >>"$TMP"
  sudo crontab "$TMP"
  rm -f "$TMP"
  echo "Installed root crontab:"
  sudo crontab -l | grep 'magicq-companion daily reboot'
}

install_user() {
  TMP=$(mktemp)
  crontab -l 2>/dev/null | grep -v 'magicq-companion daily reboot' >"$TMP" || true
  printf '%s\n' "0 14 * * * sudo -n /sbin/reboot ${MARKER}" >>"$TMP"
  crontab "$TMP"
  rm -f "$TMP"
  echo "Installed user crontab:"
  crontab -l | grep 'magicq-companion daily reboot'
}

if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  install_root
else
  install_user
fi
