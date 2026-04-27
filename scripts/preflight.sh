#!/usr/bin/env bash
set -euo pipefail

# Fast pre-flight checks for Monday pilot.
# Required env:
#   API_HEALTH_URL   (e.g. https://api.example.com/health)
#   WS_HOST          (e.g. ws.example.com)
#   DATABASE_URL
# Optional:
#   PILOT_CP_ID      default PILOT-SIM-01
#   PILOT_ID_TAG     default PILOT-001
#   PILOT_USER
#   PILOT_PASSWORD

API_HEALTH_URL="${API_HEALTH_URL:?API_HEALTH_URL is required}"
WS_HOST="${WS_HOST:?WS_HOST is required}"
DATABASE_URL="${DATABASE_URL:?DATABASE_URL is required}"
PILOT_CP_ID="${PILOT_CP_ID:-PILOT-SIM-01}"
PILOT_ID_TAG="${PILOT_ID_TAG:-PILOT-001}"

echo "[1/6] Health check"
curl -fsS "${API_HEALTH_URL}" | jq .

echo "[2/6] WSS port check"
nc -zv "${WS_HOST}" 443

echo "[3/6] Station credentials"
psql "${DATABASE_URL}" -c "SELECT station_id, username, active FROM station_credentials ORDER BY station_id LIMIT 20;"

echo "[4/6] OCPP sequences"
psql "${DATABASE_URL}" -c "SELECT last_value AS tx_id_last FROM ocpp_transaction_id; SELECT last_value AS profile_id_last FROM ocpp_charging_profile_id;"

echo "[5/6] Recent connector state"
psql "${DATABASE_URL}" -c "SELECT station_id, connector_id, status, timestamp FROM connector_status ORDER BY timestamp DESC LIMIT 20;"

if [[ -n "${PILOT_USER:-}" && -n "${PILOT_PASSWORD:-}" ]]; then
  echo "[6/6] Lifecycle simulator"
  python3 scripts/pilot_simulator.py \
    --url "wss://${WS_HOST}/ocpp/${PILOT_CP_ID}" \
    --cp-id "${PILOT_CP_ID}" \
    --user "${PILOT_USER}" \
    --pass "${PILOT_PASSWORD}" \
    --id-tag "${PILOT_ID_TAG}"
else
  echo "[6/6] Skipped simulator (set PILOT_USER and PILOT_PASSWORD to run)"
fi

echo "Pre-flight completed."

