#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# deploy_to_pi.sh — Deploy Vœrynth Sentinel to a Raspberry Pi host
# Usage: ./deploy_to_pi.sh <pi-host> [pi-user]
# You can also provide PI_HOST, PI_USER, or INSTALL_DIR via env vars.
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

PI_HOST="${1:-${PI_HOST:-}}"
PI_USER="${2:-${PI_USER:-hawatchdog}}"
INSTALL_DIR="${INSTALL_DIR:-/home/${PI_USER}/Documents/ha-watchdog}"
SERVICE_DIR="/etc/systemd/system"

if [[ -z "${PI_HOST}" ]]; then
  echo "Usage: ./deploy_to_pi.sh <pi-host> [pi-user]"
  echo "Example: ./deploy_to_pi.sh 10.0.0.25 hawatchdog"
  exit 1
fi

echo "▶ Deploying Vœrynth Sentinel to ${PI_USER}@${PI_HOST}:${INSTALL_DIR}"

ssh "${PI_USER}@${PI_HOST}" "mkdir -p '${INSTALL_DIR}/logs'"

SYNC_FILES=(
  ha_watchdog.py
  ha_watchdog_status_server.py
  runtime_config.py
  README.md
  LICENSE
  config.env.example
  assets/
)

if [[ -f config.env ]]; then
  echo "▶ Found local config.env; syncing it to target"
  SYNC_FILES+=(config.env)
fi

rsync -av --progress \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'logs/' \
  "${SYNC_FILES[@]}" \
  "${PI_USER}@${PI_HOST}:${INSTALL_DIR}/"

sed -e "s|__WATCHDOG_USER__|${PI_USER}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    ha-watchdog.service \
  | ssh "${PI_USER}@${PI_HOST}" "sudo tee '${SERVICE_DIR}/ha-watchdog.service' > /dev/null"

sed -e "s|__WATCHDOG_USER__|${PI_USER}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    ha-watchdog-status.service \
  | ssh "${PI_USER}@${PI_HOST}" "sudo tee '${SERVICE_DIR}/ha-watchdog-status.service' > /dev/null"

if ssh "${PI_USER}@${PI_HOST}" "test ! -f '${INSTALL_DIR}/config.env'"; then
  echo "▶ No config.env found on target. Seeding from config.env.example"
  ssh "${PI_USER}@${PI_HOST}" "cp '${INSTALL_DIR}/config.env.example' '${INSTALL_DIR}/config.env'"
fi

echo "▶ Installing Python dependencies..."
ssh "${PI_USER}@${PI_HOST}" "python3 -m pip install --break-system-packages requests tinytuya paramiko 2>/dev/null || python3 -m pip install requests tinytuya paramiko"

echo "▶ Enabling and starting services..."
ssh "${PI_USER}@${PI_HOST}" "
  sudo systemctl daemon-reload
  sudo systemctl enable ha-watchdog.service ha-watchdog-status.service
  sudo systemctl restart ha-watchdog.service ha-watchdog-status.service
  sleep 2
  sudo systemctl status ha-watchdog.service ha-watchdog-status.service --no-pager
"

echo
echo "✅ Deployment complete. Dashboard: http://${PI_HOST}:8080"