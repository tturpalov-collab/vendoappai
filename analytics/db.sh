#!/usr/bin/env bash
# psql через туннель. Туннель поднимается автоматически, если ещё не поднят.
#
#   ./analytics/db.sh                          — интерактивный psql
#   ./analytics/db.sh -f sql/00_discover.sql   — выполнить файл
#   ./analytics/db.sh -c "select now()"        — выполнить запрос
#   ./analytics/db.sh --csv -f sql/x.sql > out/x.csv  — выгрузка в CSV
set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] || { echo "Нет analytics/.env — скопируйте из .env.example"; exit 1; }
set -a; . ./.env; set +a

./tunnel.sh up >/dev/null

CSV=0
if [ "${1:-}" = "--csv" ]; then CSV=1; shift; fi

ARGS=(-h 127.0.0.1 -p "$LOCAL_PORT" -U "$PGUSER" -d "$PGDATABASE" -v ON_ERROR_STOP=1)
[ "$CSV" = "1" ] && ARGS+=(--csv) || ARGS+=(-P pager=off)

exec psql "${ARGS[@]}" "$@"
