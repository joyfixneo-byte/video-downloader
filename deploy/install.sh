#!/usr/bin/env bash
#
# Установка video-downloader на Ubuntu.
# Запускать из корня репозитория:  sudo bash deploy/install.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"

echo "==> Каталог приложения: $APP_DIR"
echo "==> Пользователь сервиса: $SERVICE_USER"

echo "==> Устанавливаю системные пакеты (python, ffmpeg)..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ffmpeg

echo "==> Создаю виртуальное окружение и ставлю зависимости..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt"

echo "==> Устанавливаю systemd-сервис..."
# Подставляем реальные пути/пользователя в шаблон юнита.
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    "$APP_DIR/deploy/video-downloader.service" \
    > /etc/systemd/system/video-downloader.service

systemctl daemon-reload
systemctl enable video-downloader
systemctl restart video-downloader

echo ""
echo "==> Готово. Сервис запущен на порту 8000."
echo "    Статус:  systemctl status video-downloader"
echo "    Логи:    journalctl -u video-downloader -f"
echo ""
echo "    Чтобы задать пароль доступа, добавь в юнит строку"
echo "    Environment=DOWNLOADER_PASSWORD=твой_пароль и сделай:"
echo "      sudo systemctl daemon-reload && sudo systemctl restart video-downloader"
