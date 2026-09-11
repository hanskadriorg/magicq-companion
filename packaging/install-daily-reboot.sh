#!/bin/bash
# Install weekday 14:00 host reboot (Mon–Fri) and @reboot companion start.
# Prefers root crontab for reboot; user crontab for @reboot start.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
START="$ROOT/packaging/start-companion.sh"
REBOOT_MARKER="# magicq-companion weekday reboot"
BOOT_MARKER="# magicq-companion boot"
REBOOT_ENTRY="0 14 * * 1-5 /sbin/reboot ${REBOOT_MARKER}"
BOOT_ENTRY="@reboot sleep 20 && ${START} ${BOOT_MARKER}"

install_root_reboot() {
  TMP=$(mktemp)
  sudo crontab -l 2>/dev/null \
    | grep -v 'magicq-companion daily reboot' \
    | grep -v 'magicq-companion weekday reboot' >"$TMP" || true
  printf '%s\n' "$REBOOT_ENTRY" >>"$TMP"
  sudo crontab "$TMP"
  rm -f "$TMP"
  echo "Installed root crontab:"
  sudo crontab -l | grep 'magicq-companion weekday reboot'
}

install_user_reboot() {
  TMP=$(mktemp)
  crontab -l 2>/dev/null \
    | grep -v 'magicq-companion daily reboot' \
    | grep -v 'magicq-companion weekday reboot' >"$TMP" || true
  printf '%s\n' "0 14 * * 1-5 sudo -n /sbin/reboot ${REBOOT_MARKER}" >>"$TMP"
  crontab "$TMP"
  rm -f "$TMP"
  echo "Installed user reboot crontab:"
  crontab -l | grep 'magicq-companion weekday reboot'
}

install_user_boot() {
  chmod +x "$START"
  TMP=$(mktemp)
  crontab -l 2>/dev/null | grep -v 'magicq-companion boot' >"$TMP" || true
  printf '%s\n' "$BOOT_ENTRY" >>"$TMP"
  crontab "$TMP"
  rm -f "$TMP"
  echo "Installed user boot crontab:"
  crontab -l | grep 'magicq-companion boot'
}

if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  install_root_reboot
else
  install_user_reboot
fi
install_user_boot
