#!/usr/bin/env bash
# Обновление video-downloader на сервере одной командой.
# Использование:  vdup            — обновить код и (если надо) перезапустить сервис
#                 vdup --no-restart — только git pull, без перезапуска (хватает для правок фронтенда)
set -e

REPO="$HOME/video-downloader"
cd "$REPO"

# GitHub через VPN рвёт HTTP/2 — принудительно HTTP/1.1 + увеличенный буфер.
# lowSpeed* заставляет git оборвать застрявшую передачу (а не висеть вечно).
git config http.version HTTP/1.1
git config http.postBuffer 524288000
git config http.lowSpeedLimit 1000
git config http.lowSpeedTime 15

echo "→ git pull"
BEFORE=$(git rev-parse HEAD)

# VPN-туннель то сбрасывает соединение (Recv failure), то подвисает.
# timeout убивает зависшую попытку через 60с, цикл повторяет до 8 раз.
ok=0
for i in $(seq 8); do
  if timeout 60 git pull --ff-only; then ok=1; break; fi
  echo "↻ попытка $i не прошла (VPN), повтор через 3с..."
  sleep 3
done
if [ "$ok" != "1" ]; then
  echo "✗ Не удалось подтянуть код за 8 попыток. Попробуй ещё раз чуть позже."
  exit 1
fi

AFTER=$(git rev-parse HEAD)

# yt-dlp ломается по расписанию YouTube, а не по расписанию репозитория:
# сайты меняют защиту ссылок — и старая версия отдаёт «HTTP Error 403».
# Поэтому обновляем его на каждом vdup, независимо от того, менялся ли код.
# Ошибка обновления (сеть/VPN) не должна валить весь vdup.
echo "→ проверяю обновление yt-dlp"
if timeout 180 "$REPO/.venv/bin/pip" install -U yt-dlp 2>&1 | grep -q "Successfully installed"; then
  YTDLP_NEW=1
  echo "  ✓ yt-dlp обновлён (нужен перезапуск сервиса)"
else
  YTDLP_NEW=""
  echo "  · yt-dlp уже свежий (или обновиться не вышло — проверь сеть)"
fi

if [ "$BEFORE" = "$AFTER" ] && [ -z "$YTDLP_NEW" ]; then
  echo "✓ Уже актуально, ничего не менялось."
  exit 0
fi

if [ "$1" = "--no-restart" ]; then
  echo "✓ Код обновлён (без перезапуска). Обнови страницу в браузере (Ctrl+F5)."
  exit 0
fi

# Если менялись только файлы фронтенда — перезапуск не нужен
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
if [ -n "$YTDLP_NEW" ] || echo "$CHANGED" | grep -qvE '^backend/static/'; then
  echo "→ изменения вне фронтенда, перезапускаю сервис"
  sudo systemctl restart video-downloader
  echo "✓ Обновлено и перезапущено."
else
  echo "✓ Менялся только фронтенд — перезапуск не нужен. Обнови страницу (Ctrl+F5)."
fi
