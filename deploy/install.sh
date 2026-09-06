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

echo "==> Устанавливаю таймер автообновления yt-dlp..."
# yt-dlp ломается по расписанию YouTube, а не репозитория — без таймера
# он тихо стареет в venv, пока однажды не начнёт отдавать «403 Forbidden».
sed -e "s|__APP_DIR__|$APP_DIR|g"     "$APP_DIR/deploy/video-downloader-ytdlp.service"     > /etc/systemd/system/video-downloader-ytdlp.service
cp "$APP_DIR/deploy/video-downloader-ytdlp.timer"    /etc/systemd/system/video-downloader-ytdlp.timer

echo "==> Устанавливаю сторожа (самопроверка сайта раз в минуту)..."
# Процесс может зависнуть, не упав (общий пул потоков FastAPI кончился), —
# systemd такого не видит, а снаружи это «502 Bad Gateway». Сторож раз в минуту
# дёргает /api/config и перезапускает приложение или туннель.
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/deploy/video-downloader-watchdog.service" \
    > /etc/systemd/system/video-downloader-watchdog.service
cp "$APP_DIR/deploy/video-downloader-watchdog.timer" \
   /etc/systemd/system/video-downloader-watchdog.timer

systemctl daemon-reload
systemctl enable video-downloader
systemctl restart video-downloader
systemctl enable --now video-downloader-ytdlp.timer
systemctl enable --now video-downloader-watchdog.timer

echo ""
echo "==> Готово. Сервис запущен на порту 8000."
echo "    Статус:  systemctl status video-downloader"
echo "    Логи:    journalctl -u video-downloader -f"
echo "    Автообновление yt-dlp: systemctl list-timers | grep ytdlp"
echo "    Сторож:  journalctl -u video-downloader-watchdog -f"
echo ""
echo "    Чтобы сторож ловил и обрыв туннеля (а не только зависший сайт),"
echo "    раскомментируй в /etc/systemd/system/video-downloader-watchdog.service"
echo "    строку WATCHDOG_PUBLIC_URL со своим внешним адресом (см. комментарий там)."
echo ""
echo "    Чтобы задать пароль доступа, добавь в юнит строку"
echo "    Environment=DOWNLOADER_PASSWORD=твой_пароль и сделай:"
echo "      sudo systemctl daemon-reload && sudo systemctl restart video-downloader"
