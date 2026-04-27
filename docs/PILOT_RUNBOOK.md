# OCPP Pilot Runbook

## Fast pre-flight (<60s)

Run:

```bash
API_HEALTH_URL="https://<api-host>/health" \
WS_HOST="<ws-host>" \
DATABASE_URL="<postgres-uri>" \
PILOT_USER="<basic-auth-user>" \
PILOT_PASSWORD="<basic-auth-password>" \
./scripts/preflight.sh
```

Checks performed:
1. API health endpoint
2. WSS socket reachability (`443`)
3. `station_credentials` entries
4. OCPP DB sequences (`ocpp_transaction_id`, `ocpp_charging_profile_id`)
5. Latest `connector_status` rows
6. End-to-end OCPP lifecycle simulator

## Direct simulator run

```bash
python3 scripts/pilot_simulator.py \
  --url "wss://<ws-host>/ocpp/<cp-id>" \
  --cp-id "<cp-id>" \
  --user "<basic-auth-user>" \
  --pass "<basic-auth-password>" \
  --connector 1 \
  --id-tag "PILOT-001"
```

Expected outcome:
- Exit code `0`
- Logs include `Lifecycle complete`
- `SetChargingProfile` received with `chargingRateUnit=A`

