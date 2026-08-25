#!/usr/bin/env bash
#
# Обновление yt-dlp на сервере.
#
# Запускается systemd-таймером раз в сутки (video-downloader-ytdlp.timer)
# и заодно из vdup. Живёт отдельно от кода приложения намеренно: yt-dlp
# ломается по расписанию YouTube, а не репозитория — сайты меняют защиту
# ссылок, и старая версия начинает отдавать «HTTP Error 403: Forbidden».
# Ждать, пока пользователь сам вспомнит про pip, оказалось нельзя: на VM
# спокойно прожил yt-dlp пятимесячной давности.
#
# Запускать только от root: .venv создан через `sudo bash install.sh` и
# принадлежит root (обычный pip упирается в Permission denied), а рестарт
# сервиса всё равно требует прав.
#
# set -e тут НЕ нужен: отвалившаяся сеть/VPN — обычное дело, из-за неё
# юнит не должен краснеть. Все важные ошибки разбираются явно.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP="$APP_DIR/.venv/bin/pip"
STAMP="$APP_DIR/backend/data/ytdlp_checked"
SERVICE="video-downloader"
# Внутренний порт приложения одинаков на обеих VM (наружу их разводит frp).
PORT="${VD_PORT:-8000}"

if [ "$(id -u)" != "0" ]; then
  echo "✗ Запускать нужно от root:  sudo $0"
  exit 1
fi

echo "→ проверяю обновление yt-dlp"
OUT=$(timeout 300 "$PIP" install -U yt-dlp 2>&1)
echo "$OUT" | tail -n 3

mkdir -p "$(dirname "$STAMP")"

if ! echo "$OUT" | grep -q "Successfully installed"; then
  # Порядок проверок важен: при удачном обновлении pip печатает и
  # «Requirement already satisfied», и «Successfully installed» — поэтому
  # сюда попадаем, только если ничего не поставилось.
  if echo "$OUT" | grep -q "already satisfied"; then
    touch "$STAMP"      # проверка состоялась, обновлять было нечего
    echo "✓ yt-dlp уже свежий"
  else
    echo "⚠ обновиться не вышло (сеть/VPN?) — таймер попробует завтра"
  fi
  exit 0
fi

touch "$STAMP"
echo "✓ yt-dlp обновлён"

# Новая версия уже в venv, но процесс держит в памяти импорт старой —
# применится только после рестарта. Рестартовать посреди чужой загрузки
# нельзя: обычная yt-dlp-загрузка после обрыва не возобновляется, а
# транскрибация Whisper пропадёт вообще молча (торренты-то поднимутся сами).
# Спрашиваем у самого приложения — оно одно знает про все виды задач сразу.
if curl -s -m 5 "http://127.0.0.1:$PORT/api/config" | grep -q '"busy":true'; then
  echo "· идёт загрузка — перезапуск отложен до следующей проверки"
  echo "  (на сайте внизу видно, что версия ждёт перезапуска; ближайший vdup её применит)"
  exit 0
fi

# Сюда же попадаем, если сервис вообще не ответил — тогда рестарт тем более уместен.
systemctl restart "$SERVICE"
echo "✓ $SERVICE перезапущен на новой версии"
