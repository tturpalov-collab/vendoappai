#!/usr/bin/env bash
# Поднимает SSH-туннель до RDS и держит его в фоне.
#   ./analytics/tunnel.sh up      — поднять
#   ./analytics/tunnel.sh down    — погасить
#   ./analytics/tunnel.sh status  — проверить
set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] || { echo "Нет analytics/.env — скопируйте из .env.example"; exit 1; }
set -a; . ./.env; set +a

PIDFILE="/tmp/vendo-tunnel-${LOCAL_PORT}.pid"

is_up() { nc -z 127.0.0.1 "$LOCAL_PORT" >/dev/null 2>&1; }

case "${1:-up}" in
  up)
    if is_up; then echo "Туннель уже поднят на порту $LOCAL_PORT"; exit 0; fi
    ssh -f -N \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -i "$SSH_KEY" \
        -L "${LOCAL_PORT}:${RDS_HOST}:${RDS_PORT}" \
        "${SSH_USER}@${SSH_HOST}"
    pgrep -f "L ${LOCAL_PORT}:${RDS_HOST}" > "$PIDFILE" 2>/dev/null || true
    sleep 1
    is_up && echo "Туннель поднят: 127.0.0.1:${LOCAL_PORT} -> ${RDS_HOST}:${RDS_PORT}" \
          || { echo "Туннель не поднялся"; exit 1; }
    ;;
  down)
    pkill -f "L ${LOCAL_PORT}:${RDS_HOST}" && echo "Туннель погашен" || echo "Активного туннеля не найдено"
    rm -f "$PIDFILE"
    ;;
  status)
    is_up && echo "UP  (127.0.0.1:${LOCAL_PORT})" || echo "DOWN"
    ;;
  *)
    echo "Использование: $0 {up|down|status}"; exit 1;;
esac
