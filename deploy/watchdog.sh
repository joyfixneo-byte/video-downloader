#!/usr/bin/env bash
#
# Сторож video-downloader: раз в минуту проверяет, что сайт реально отвечает,
# и перезапускает то, что сломалось.
#
# Зачем: снаружи периодически ловится «502 Bad Gateway», а сам процесс при
# этом systemd-у кажется живым (Restart=on-failure тут не помогает — падения
# нет). Все ручки приложения синхронные и крутятся в общем пуле из 40 потоков:
# одного зависшего ffprobe, мёртвого SMB-маунта или залипшего скрейпера хватает,
# чтобы пул кончился и сайт перестал отвечать, не упав.
#
# Поэтому пробуем не «открыт ли порт», а живую ручку /api/config: она без
# пароля, но дёргает JOBS_LOCK и диск — то есть отвечает только по-настоящему
# работающее приложение.
#
# Две пробы, два разных лечения:
#   локально мёртво              → перезапуск самого приложения;
#   локально живо, снаружи 502   → виноват frp-туннель, перезапуск frpc.
#
# Занятость (busy) намеренно не спрашиваем, в отличие от ytdlp-update.sh:
# раз сайт отдаёт 502, управлять загрузкой всё равно нельзя. Торренты после
# рестарта поднимутся сами (_resume_interrupted_torrents), yt-dlp-загрузка
# потеряется — это осознанный размен на живой сайт.
#
# Запускается systemd-таймером (video-downloader-watchdog.timer) от root:
# systemctl restart без root не сделать.
#
# set -e тут НЕ нужен: отвалившаяся проба — штатная ситуация, из-за неё юнит
# не должен краснеть. Все важные ошибки разбираются явно.
set -uo pipefail

SERVICE="${WATCHDOG_SERVICE:-video-downloader}"
# Внутренний порт одинаков на обеих VM (наружу их разводит frp).
PORT="${VD_PORT:-8000}"
LOCAL_URL="${WATCHDOG_LOCAL_URL:-http://127.0.0.1:$PORT/api/config}"
# Внешний адрес (через VPS+туннель) — пустой по умолчанию: он разный на двух
# машинах (:8000 у apps-vpn, :8002 у apps), а файл юнита один. Задаётся строкой
# Environment=WATCHDOG_PUBLIC_URL=... в юните. Пусто — внешняя проба выключена,
# frpc не трогаем.
PUBLIC_URL="${WATCHDOG_PUBLIC_URL:-}"
# Сколько провалов подряд терпим, прежде чем чинить. При тике раз в минуту это
# ~3 минуты простоя, зато одиночный тормоз (ffprobe на большом файле, всплеск
# загрузки) не рвёт сервис на ровном месте.
FAILS="${WATCHDOG_FAILS:-3}"
LOCAL_TIMEOUT="${WATCHDOG_LOCAL_TIMEOUT:-10}"
PUBLIC_TIMEOUT="${WATCHDOG_PUBLIC_TIMEOUT:-15}"
# Счётчики провалов: /run — tmpfs, чистится при перезагрузке и не трогает диск.
STATE_DIR="${WATCHDOG_STATE_DIR:-/run/video-downloader-watchdog}"

mkdir -p "$STATE_DIR"

# Проба URL. Печатает человеческий диагноз, возвращает 0 только на 200.
probe() {
  local url=$1 tmo=$2 code rc
  code=$(curl -s -o /dev/null -m "$tmo" -w '%{http_code}' "$url")
  rc=$?
  if [ "$rc" = 0 ] && [ "$code" = 200 ]; then echo "ок"; return 0; fi
  case "$rc" in
    0)  echo "ответил кодом $code" ;;
    28) echo "не ответил за ${tmo}с (завис)" ;;
    7)  echo "соединение отклонено (процесс не слушает порт)" ;;
    *)  echo "curl не смог (код $rc)" ;;
  esac
  return 1
}

bump() {   # $1 — имя счётчика; печатает новое значение
  local f="$STATE_DIR/$1" n
  n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$f"
  echo "$n"
}

reset() { echo 0 > "$STATE_DIR/$1"; }

# Юнит туннеля ищем сами: на разных машинах он может называться frpc.service,
# frp.service, frpc@что-то.service. list-unit-files видит и остановленный юнит
# (list-units — уже нет), а это как раз наш случай.
frpc_unit() {
  if [ -n "${WATCHDOG_FRPC_UNIT:-}" ]; then echo "$WATCHDOG_FRPC_UNIT"; return; fi
  systemctl list-unit-files --no-legend --no-pager 'frp*.service' 2>/dev/null \
    | awk '{print $1}' | head -n 1
}

# --- 1. Локальная проба: живо ли само приложение --------------------------
if msg=$(probe "$LOCAL_URL" "$LOCAL_TIMEOUT"); then
  reset local
else
  n=$(bump local)
  echo "· локально $LOCAL_URL — $msg (провал $n из $FAILS)"
  if [ "$n" -ge "$FAILS" ]; then
    echo "→ перезапускаю $SERVICE"
    if systemctl restart "$SERVICE"; then
      echo "✓ $SERVICE перезапущен"
    else
      echo "✗ перезапустить $SERVICE не вышло — смотри systemctl status $SERVICE"
    fi
    reset local
    # Внешний счётчик тоже в ноль: пока приложение лежало, внешняя проба
    # проваливалась по его вине, а не по вине туннеля.
    reset public
  fi
  exit 0
fi

# --- 2. Внешняя проба: доходит ли до сайта через VPS и туннель -------------
[ -n "$PUBLIC_URL" ] || exit 0

if msg=$(probe "$PUBLIC_URL" "$PUBLIC_TIMEOUT"); then
  reset public
  exit 0
fi

n=$(bump public)
echo "· снаружи $PUBLIC_URL — $msg, хотя локально сайт отвечает (провал $n из $FAILS)"
[ "$n" -ge "$FAILS" ] || exit 0

unit=$(frpc_unit)
if [ -z "$unit" ]; then
  echo "✗ юнит туннеля не найден (искал по маске frp*.service). Задай"
  echo "  Environment=WATCHDOG_FRPC_UNIT=имя.service в юните сторожа или чини руками."
else
  echo "→ перезапускаю туннель $unit"
  if systemctl restart "$unit"; then
    echo "✓ $unit перезапущен"
  else
    echo "✗ перезапустить $unit не вышло — смотри systemctl status $unit"
  fi
fi
# Счётчик в ноль в любом случае: следующая починка — не раньше чем через
# ещё $FAILS провалов подряд, иначе при неверном WATCHDOG_PUBLIC_URL сторож
# дёргал бы туннель каждую минуту.
reset public
