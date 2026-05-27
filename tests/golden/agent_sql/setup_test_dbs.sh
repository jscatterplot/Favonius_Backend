#!/usr/bin/env bash
# Stand up the TimescaleDB + Supabase test pair the agent-SQL golden gate needs,
# locally, in one TimescaleDB container hosting two databases:
#
#   favonius_test    — migrations/*.sql            (agent_views.* TS functions, 042)
#   favonius_static  — supabase_bootstrap.sql       (Supabase-owned base tables)
#                      + migrations/supabase/040_agent_views_static.sql
#
# The static base tables (sites/vehicles/charging_stations/drivers/schedules)
# are owned by the Supabase project, not this repo, so they are reconstructed by
# supabase_bootstrap.sql; migration 040 (the agent_views.* ground truth) is then
# applied verbatim on top. See supabase_bootstrap.sql for the rationale.
#
# Then run:  pytest -m agent_sql_golden
#
# Env:
#   PYTHON   python with the project's deps (asyncpg). Default: .venv/bin/python
#            if present, else python3.
#   PGPORT   host port for the container. Default 5433.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

PGPORT="${PGPORT:-5433}"
CONTAINER="${CONTAINER:-fav-agent-sql}"
IMAGE="timescale/timescaledb:latest-pg16"
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then PYTHON="$REPO_ROOT/.venv/bin/python"; else PYTHON="python3"; fi
fi

TS_URL="postgresql://favonius_test:test_password@localhost:${PGPORT}/favonius_test"
STATIC_URL="postgresql://favonius_test:test_password@localhost:${PGPORT}/favonius_static"

echo "==> (re)starting $CONTAINER on :$PGPORT"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -p "${PGPORT}:5432" \
  -e POSTGRES_USER=favonius_test -e POSTGRES_PASSWORD=test_password -e POSTGRES_DB=favonius_test \
  "$IMAGE" >/dev/null

echo "==> waiting for postgres (the timescaledb image restarts once during init)"
# pg_isready can report ready against the transient init-time server, which the
# image then restarts to load its preload library — connecting in that window
# gives "connection reset by peer". Require several consecutive successful
# real queries, then settle, so the restart is safely behind us.
ok=0
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" psql -U favonius_test -d favonius_test -tAc "SELECT 1" >/dev/null 2>&1; then
    ok=$((ok + 1)); [ "$ok" -ge 3 ] && break
  else
    ok=0
  fi
  sleep 1
done
sleep 2

echo "==> creating favonius_static"
docker exec "$CONTAINER" psql -U favonius_test -d favonius_test -c \
  "CREATE DATABASE favonius_static OWNER favonius_test;" >/dev/null

echo "==> applying TS migrations -> favonius_test"
DATABASE_URL="$TS_URL" "$PYTHON" scripts/run_migrations.py >/dev/null
echo "    ok"

echo "==> applying supabase base + agent_views (040 + 045 depots zone fallback) -> favonius_static"
docker exec -i "$CONTAINER" psql -U favonius_test -d favonius_static -v ON_ERROR_STOP=1 \
  < tests/golden/agent_sql/supabase_bootstrap.sql >/dev/null
docker exec -i "$CONTAINER" psql -U favonius_test -d favonius_static -v ON_ERROR_STOP=1 \
  < migrations/supabase/040_agent_views_static.sql >/dev/null
docker exec -i "$CONTAINER" psql -U favonius_test -d favonius_static -v ON_ERROR_STOP=1 \
  < migrations/supabase/045_agent_depots_entsoe_zone_fallback.sql >/dev/null
echo "    ok"

echo
echo "ready. export these and run the gate:"
echo "  export TEST_DATABASE_URL='$TS_URL'"
echo "  export TEST_STATIC_DATABASE_URL='$STATIC_URL'"
echo "  pytest -m agent_sql_golden"
