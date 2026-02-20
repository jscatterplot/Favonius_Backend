#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.everest.yml}"
PROJECT_NAME="${PROJECT_NAME:-favonius-everest-smoke}"
API_HEALTH_URL="${API_HEALTH_URL:-http://localhost:18000/health}"
CHARGE_POINT_ID="${CHARGE_POINT_ID:-${EVEREST_CHARGE_POINT_ID:-charger_01}}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-240}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
KEEP_STACK="${KEEP_STACK:-0}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

cleanup() {
  echo "Stopping EVerest smoke-test stack..."
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" \
    down -v --remove-orphans >/dev/null 2>&1 || true
}

require_cmd docker
require_cmd curl
require_cmd python3
require_cmd rg

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

if [[ "$KEEP_STACK" != "1" ]]; then
  trap cleanup EXIT
fi

echo "Starting EVerest + backend stack..."
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --build \
  timescaledb api everest-mqtt everest-manager

echo "Waiting for backend health and OCPP connection..."
deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if curl -fsS "$API_HEALTH_URL" 2>/dev/null | python3 -c '
import json
import sys

data = json.load(sys.stdin)
components = data.get("components", {})
database_ok = components.get("database") == "healthy"
ocpp_ok = components.get("ocpp_server") == "healthy"
sys.exit(0 if database_ok and ocpp_ok else 1)
'; then
    echo "Health check confirms DB + OCPP charger connection."
    break
  fi

  sleep "$POLL_INTERVAL_SECONDS"
done

if (( SECONDS >= deadline )); then
  echo "Timed out waiting for healthy backend and connected EVerest charger." >&2
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" logs --no-color api everest-manager
  exit 1
fi

if ! docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" logs --no-color api \
  | rg -Fq "BootNotification from ${CHARGE_POINT_ID}"; then
  echo "BootNotification from ${CHARGE_POINT_ID} not found in API logs." >&2
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" logs --no-color api
  exit 1
fi

echo "EVerest smoke test passed: backend communicated with charge point ${CHARGE_POINT_ID}."

if [[ "$KEEP_STACK" == "1" ]]; then
  echo "KEEP_STACK=1 set; stack remains running."
fi

