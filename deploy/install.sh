#!/usr/bin/env bash
set -euo pipefail

APP_USER="tradingbot"
APP_DIR="/home/${APP_USER}/app"

if ! id "${APP_USER}" >/dev/null 2>&1; then
  sudo useradd -m -s /bin/bash "${APP_USER}"
fi

sudo mkdir -p "${APP_DIR}"
sudo chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

sudo -u "${APP_USER}" bash -lc "cd \"${APP_DIR}\" && python3.11 -m venv .venv"
sudo -u "${APP_USER}" bash -lc "cd \"${APP_DIR}\" && .venv/bin/pip install -U pip"
sudo -u "${APP_USER}" bash -lc "cd \"${APP_DIR}\" && .venv/bin/pip install -e ."

sudo cp deploy/tradingbot.service /etc/systemd/system/tradingbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot

echo "Installed. Check logs with: sudo journalctl -u tradingbot -f"
