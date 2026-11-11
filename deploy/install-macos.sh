#!/usr/bin/env bash
# Автозапуск на macOS через launchd: один раз запустил — работает всегда.
#   bash deploy/install-macos.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="uz.tsue.lmsauto"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$APP_DIR/.env" ] || { echo "Нет .env — впиши HEMIS_LOGIN и HEMIS_PASSWORD."; exit 1; }
[ -f "$APP_DIR/lms_auto.py" ] || { echo "Нет lms_auto.py в $APP_DIR"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"

# снять старую версию и добить nohup-процессы, иначе будут дубли
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
pkill -f "lms_auto.py" 2>/dev/null || true
sleep 1

sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/$LABEL.plist" > "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

sleep 3
echo "Установлено: $PLIST"
launchctl list | grep "$LABEL" || true
echo
echo "Логи:   tail -f $APP_DIR/lms.log"
echo "Статус: launchctl print gui/$(id -u)/$LABEL | head -20"
echo "Стоп:   launchctl bootout gui/$(id -u)/$LABEL"
