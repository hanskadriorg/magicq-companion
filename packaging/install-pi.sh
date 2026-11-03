#!/bin/bash
# Install MagicQ Companion on a Raspberry Pi (systemd --ui on boot).
# Run on the Pi, from the repo root or as:
#   curl -fsSL https://raw.githubusercontent.com/hanskadriorg/magicq-companion/master/packaging/install-pi.sh | bash
# Or, after cloning:
#   bash packaging/install-pi.sh
set -euo pipefail

REPO_URL="${MAGICQ_REPO_URL:-https://github.com/hanskadriorg/magicq-companion.git}"
INSTALL_DIR="${MAGICQ_INSTALL_DIR:-$HOME/magicq-companion}"
SERVICE_NAME="magicq-companion"
UI_PORT="${MAGICQ_UI_PORT:-8765}"

echo "==> Installing MagicQ Companion into ${INSTALL_DIR}"

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  git curl ca-certificates \
  python3 python3-venv python3-pip \
  libportaudio2 alsa-utils

if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 20)'; then
  echo "==> Installing Node.js 20+"
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  git clone "${REPO_URL}" "${INSTALL_DIR}"
else
  git -C "${INSTALL_DIR}" pull --ff-only || true
fi
cd "${INSTALL_DIR}"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

npm install --prefix matter_sidecar
npm run build --prefix matter_sidecar

# Dashboard Reboot button needs passwordless reboot.
SUDOERS_FILE="/etc/sudoers.d/magicq-companion"
echo "${USER} ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/sbin/reboot" | sudo tee "${SUDOERS_FILE}" >/dev/null
sudo chmod 440 "${SUDOERS_FILE}"

if [[ -x packaging/install-daily-reboot.sh ]]; then
  bash packaging/install-daily-reboot.sh || true
fi

# LAN access: ufw on Raspberry Pi OS often blocks 8765 from other machines.
if [[ -x packaging/open-dashboard-port.sh ]]; then
  bash packaging/open-dashboard-port.sh "${UI_PORT}" || true
fi

UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
sudo tee "${UNIT}" >/dev/null <<EOF
[Unit]
Description=MagicQ Companion (audio analysis + Matter + web UI)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
Group=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m magicq_companion --ui --ui-port ${UI_PORT}
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Installed."
echo "  On this Pi:     http://127.0.0.1:${UI_PORT}"
if [[ -n "${LAN_IP}" ]]; then
  echo "  From LAN/FOH:   http://${LAN_IP}:${UI_PORT}"
fi
echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo "Edit ${INSTALL_DIR}/config.toml then: sudo systemctl restart ${SERVICE_NAME}"
echo "If LAN still fails: bash ${INSTALL_DIR}/packaging/open-dashboard-port.sh ${UI_PORT}"
