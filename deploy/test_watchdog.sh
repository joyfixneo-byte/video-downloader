#!/usr/bin/env bash
#
# Самопроверка сторожа: подсовываем ему поддельные curl и systemctl и смотрим,
# кого он перезапускает. Без сети, без root, без systemd — гоняется где угодно,
# в том числе на Windows в Git Bash:
#
#   bash deploy/test_watchdog.sh
#
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

mkdir -p "$TMP/bin"

# Поддельный curl: отвечает так, как велят STUB_LOCAL / STUB_PUBLIC.
cat > "$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
url=${!#}
case "$url" in
  *127.0.0.1*) mode=$STUB_LOCAL ;;
  *)           mode=$STUB_PUBLIC ;;
esac
case "$mode" in
  200)     printf 200; exit 0 ;;
  500)     printf 500; exit 0 ;;
  timeout) printf 000; exit 28 ;;
  refused) printf 000; exit 7 ;;
esac
exit 1
EOF

# Поддельный systemctl: пишет в лог, кого просили перезапустить.
cat > "$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  restart)          echo "$2" >> "$STUB_LOG" ;;
  list-unit-files)  echo "frpc.service enabled enabled" ;;
esac
exit 0
EOF

chmod +x "$TMP/bin/curl" "$TMP/bin/systemctl"
export PATH="$TMP/bin:$PATH"

# Прогнать N тиков сторожа с заданными ответами проб и вернуть, кого он чинил.
run() {   # $1 — сколько тиков, $2 — STUB_LOCAL, $3 — STUB_PUBLIC, $4 — внешний URL
  local ticks=$1
  export STUB_LOCAL=$2 STUB_PUBLIC=$3
  export WATCHDOG_PUBLIC_URL=${4:-}
  export WATCHDOG_STATE_DIR="$TMP/state" STUB_LOG="$TMP/restarts"
  rm -rf "$TMP/state" "$TMP/restarts"
  for _ in $(seq "$ticks"); do bash "$DIR/watchdog.sh" >/dev/null; done
  cat "$TMP/restarts" 2>/dev/null | tr '\n' ' '
}

check() {   # $1 — что проверяем, $2 — ожидаемый лог рестартов, $3 — фактический
  if [ "$2" = "$3" ]; then
    echo "  ✓ $1"
  else
    echo "  ✗ $1: ожидали «$2», получили «$3»"
    FAILED=1
  fi
}

PUB="http://example.invalid/api/config"

echo "Сторож:"
check "всё живо — никого не трогаем" \
      "" "$(run 3 200 200 "$PUB")"
check "два провала подряд — ещё рано чинить" \
      "" "$(run 2 timeout 200 "$PUB")"
check "три провала локально — перезапуск приложения (ровно один раз)" \
      "video-downloader " "$(run 3 timeout 200 "$PUB")"
check "порт закрыт, три тика — тоже перезапуск приложения" \
      "video-downloader " "$(run 3 refused 200 "$PUB")"
check "локально живо, снаружи мёртво — перезапуск туннеля, а не сервиса" \
      "frpc.service " "$(run 3 200 refused "$PUB")"
check "внешняя проба не настроена — туннель не трогаем" \
      "" "$(run 3 200 refused "")"
check "сайт отвечает 500 — это тоже поломка, чиним приложение" \
      "video-downloader " "$(run 3 500 200 "$PUB")"
check "шесть провалов подряд — чиним не чаще чем раз в три тика" \
      "video-downloader video-downloader " "$(run 6 timeout 200 "$PUB")"

[ "$FAILED" = 0 ] && echo "Все проверки прошли." || echo "ЕСТЬ ПРОВАЛЫ."
exit "$FAILED"
