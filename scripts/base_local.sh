#!/usr/bin/env bash
# Levanta (o reinicia) la base LOCAL de prueba de Logos en el puerto 55440.
# Nunca toca Supabase. Uso: scripts/base_local.sh [reset]
set -euo pipefail
export LC_ALL=en_US.UTF-8
cd "$(dirname "$0")/.."
PG=/opt/homebrew/opt/postgresql@17/bin
DATA=.pgdata
PORT=55440
URL="postgresql://logos@127.0.0.1:${PORT}/logos"

if [ ! -d "$DATA" ]; then
  "$PG/initdb" -D "$DATA" -U logos --auth=trust -E UTF8 --locale=en_US.UTF-8 >/dev/null
fi
if ! "$PG/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then
  "$PG/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp" -l "$DATA/server.log" start >/dev/null
  sleep 1
fi

if [ "${1:-}" = "reset" ]; then
  "$PG/dropdb" -h 127.0.0.1 -p $PORT -U logos --if-exists logos
fi
if ! "$PG/psql" -h 127.0.0.1 -p $PORT -U logos -lqt | cut -d'|' -f1 | grep -qw logos; then
  "$PG/createdb" -h 127.0.0.1 -p $PORT -U logos logos
  "$PG/psql" "$URL" -q -v ON_ERROR_STOP=1 -c "alter database logos set search_path = public, extensions"
  "$PG/psql" "$URL" -q -v ON_ERROR_STOP=1 -f scripts/supabase_local_shim.sql
  for f in supabase/migrations/*.sql; do
    "$PG/psql" "$URL" -q -v ON_ERROR_STOP=1 -f "$f"
  done
  echo "Base creada y esquema aplicado."
fi
echo "$URL"
