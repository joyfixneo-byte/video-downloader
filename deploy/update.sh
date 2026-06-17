#!/usr/bin/env bash
# Обновление video-downloader на сервере одной командой.
# Использование:  vdup            — обновить код и (если надо) перезапустить сервис
#                 vdup --no-restart — только git pull, без перезапуска (хватает для правок фронтенда)
set -e

REPO="$HOME/video-downloader"
cd "$REPO"

# GitHub через VPN рвёт HTTP/2 — принудительно HTTP/1.1 (на всякий случай)
git config http.version HTTP/1.1

echo "→ git pull"
BEFORE=$(git rev-parse HEAD)
git pull --ff-only
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "✓ Уже актуально, ничего не менялось."
  exit 0
fi

if [ "$1" = "--no-restart" ]; then
  echo "✓ Код обновлён (без перезапуска). Обнови страницу в браузере (Ctrl+F5)."
  exit 0
fi

# Если менялись только файлы фронтенда — перезапуск не нужен
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
if echo "$CHANGED" | grep -qvE '^backend/static/'; then
  echo "→ изменения вне фронтенда, перезапускаю сервис"
  sudo systemctl restart video-downloader
  echo "✓ Обновлено и перезапущено."
else
  echo "✓ Менялся только фронтенд — перезапуск не нужен. Обнови страницу (Ctrl+F5)."
fi
