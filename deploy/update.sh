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
