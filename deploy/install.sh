#!/usr/bin/env bash
# Установка автозахода как systemd-сервиса (Linux-сервер).
# Запуск: bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="lms-auto"
RUN_USER="${SUDO_USER:-$USER}"

echo "Каталог: $APP_DIR"
echo "Пользователь сервиса: $RUN_USER"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "Нет файла .env — создай его из env.example и впиши логин/пароль HEMIS."
  exit 1
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m playwright install chromium

sudo cp "$APP_DIR/deploy/lms-auto.service" "/etc/systemd/system/$SERVICE.service"
sudo sed -i "s|__APP_DIR__|$APP_DIR|g; s|__USER__|$RUN_USER|g" "/etc/systemd/system/$SERVICE.service"

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"

sleep 5
systemctl --no-pager --lines=15 status "$SERVICE" || true
echo
echo "Готово. Логи: tail -f $APP_DIR/lms.log"
