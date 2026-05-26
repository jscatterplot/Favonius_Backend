# OCPP Pilot Runbook

Operational runbook for bringing a physical OCPP 1.6 charger online against the
Favonius backend and triaging the failure modes seen during a pilot (HRX Vilnius
ABB Terra AC). It is grounded in two scripts — `scripts/preflight.sh` and
`scripts/pilot_simulator.py` — and the real operational tables
(`station_credentials`, `connector_status`, `charging_command_queue`,
`charging_sessions`).

Two services back a pilot and share one TimescaleDB instance:

- **API service** — `src/api/main.py` (REST + admin endpoints), runs with
  `OCPP_SERVER_ENABLED=false`.
- **WebSocket Handler** — `src/websocket_handler/main.py`, terminates the charger
  WebSocket and drains `charging_command_queue`.

A charger that authenticates but never charges is almost always one of the four
failure modes in the triage section below. Work top-to-bottom.

---

## Pre-flight checklist

Before pointing a physical charger at the backend, confirm:

- [ ] Both services are deployed and `GET /health` returns `200` with DB +
      (where applicable) Gurobi healthy.
- [ ] The depot exists and the charger is registered (`charging_stations` row),
      with the correct `station_id` (the OCPP id the charger dials) and
      `connector_type = 'CCS'` (MVP constraint).
- [ ] Basic Auth credentials are minted and **active** for that `station_id`
      (`station_credentials.active = TRUE`). Verify via
      `GET /admin/depots/{id}/chargers/{cid}/credentials_status` (returns
      `{configured, created_at, last_rotated_at}` — never plaintext). If missing
      or rotated, mint via
      `POST /admin/depots/{id}/chargers/{cid}/rotate_credentials`.
- [ ] OCPP id sequences exist and are sane (`ocpp_transaction_id`,
      `ocpp_charging_profile_id`; migration 012).
- [ ] The charger URL is `wss://<ws-host>/ocpp/<station_id>` and the WSS port
      (443) is reachable from the charger's network.
- [ ] Price feeder is populating data for the depot's bidding zone (so the
      optimizer doesn't fall back to the $0.15/kWh default), and at least one
      vehicle `id_tag` is registered for the tap-to-start test.
- [ ] You can reach the TimescaleDB (`DATABASE_URL`) and run `psql` for triage.

Run the automated pre-flight (below) — it mechanically checks items 1–6.

---

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

Optional overrides: `PILOT_CP_ID` (default `PILOT-SIM-01`), `PILOT_ID_TAG`
(default `PILOT-001`). The simulator step (6) is skipped unless both
`PILOT_USER` and `PILOT_PASSWORD` are set.

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

The simulator drives the full lifecycle: connect → BootNotification →
StatusNotification(Available) → Authorize → StartTransaction → 3× MeterValues →
wait for `SetChargingProfile(TxProfile, A)` → 3× MeterValues → StopTransaction →
disconnect.

Expected outcome:
- Exit code `0`
- Logs include `Lifecycle complete`
- `SetChargingProfile` received with `chargingRateUnit=A`

Non-zero exit codes map to the stage that failed and tell you where to look:

| Exit | Stage | Likely cause |
|---|---|---|
| `2` | Subprotocol negotiation | Server didn't negotiate `ocpp1.6` — wrong URL/route or proxy stripping the subprotocol |
| `3` | BootNotification | Auth rejected, or boot interval < 60s (see "Charger won't boot") |
| `4` | Authorize | `id_tag` not in `vehicles.id_tag` (see "Authorize/StartTransaction rejected") |
| `5` | StartTransaction | idTag rejected at transaction start, or sequence/session issue |
| `1` | Assertions failed | Lifecycle ran but an assertion failed — commonly a SetChargingProfile timeout (see "Queue not draining") |

---

## Failure-mode triage

Each mode lists the symptom, the SQL to confirm it, and the fix. Replace
`<sid>` with the charger's `station_id` (the OCPP id) throughout.

### 1. Charger won't boot / reconnect-loops

**Symptom:** the charger connects then drops every ~10–60s; no transactions; the
simulator exits `2` or `3`.

**Confirm:**

```sql
-- Latest connector state for this charger
SELECT station_id, connector_id, status, error_code, timestamp
FROM connector_status WHERE station_id = '<sid>'
ORDER BY timestamp DESC LIMIT 10;

-- Failed-auth events emitted by the WS handler (migration 017)
SELECT station_id, event_type, tech_info, timestamp
FROM security_events WHERE station_id = '<sid>'
ORDER BY timestamp DESC LIMIT 10;
```

**Causes & fixes:**
- **Auth failure** (see mode 4 below) — a charger that can't authenticate never
  completes BootNotification.
- **Local-auth-list bootstrap wedge.** On ABB Terra AC V1.8.x firmware the
  post-boot `SendLocalList` exchange can stall and the charger reconnect-loops.
  The send path is `src/adapters/ocpp/local_auth_sync.py`, driven from the
  legacy handler's `OCPP16Session._delayed_local_auth_sync`. Mitigation: the kill
  switch **`OCPP_DISABLE_LOCAL_AUTH_LIST=true`** falls the charger back to pure
  central Authorize without a code change — set it, restart the WS handler, and
  confirm the boot completes. Inspect what was last attempted:

  ```sql
  SELECT station_id, local_list_version, local_list_last_status,
         local_list_supported, local_list_probed_firmware, local_list_synced_at
  FROM charging_stations WHERE station_id = '<sid>';
  ```

  A `local_list_last_status` of `UnsupportedFromBootstrap` /
  `UnsupportedFeatureProfile` / `NotSupported` is the firmware-scoped negative
  cache doing its job — the charger boots fine on central Authorize.
- **Boot interval too low** — the simulator asserts `interval >= 60`; a real
  charger configured with a tiny heartbeat interval can thrash. Check the
  BootNotification response interval in the WS handler logs.

### 2. No telemetry after a transaction starts

**Symptom:** Authorize + StartTransaction succeed, but the depot state shows no
SoC/power and `telemetry` is empty for the session.

**Confirm:**

```sql
-- Is there an open session?
SELECT id, station_id, transaction_id, start_time, end_time, last_seen_at
FROM charging_sessions WHERE station_id = '<sid>'
ORDER BY start_time DESC LIMIT 5;

-- Did any MeterValues land?
SELECT time, station_id, transaction_id, charging_kw, soc, is_plugged
FROM telemetry WHERE station_id = '<sid>'
ORDER BY time DESC LIMIT 20;
```

**Causes & fixes:**
- **Charger not sending MeterValues** — many chargers gate periodic
  MeterValues on config keys (`MeterValueSampleInterval`,
  `MeterValuesSampledData`). Confirm via WS-handler logs that MeterValues frames
  arrive at all; if none do, it's a charger-side config / measurand issue.
- **Stale data, optimizer in degraded mode** — telemetry older than 15 min
  (`MAX_TELEMETRY_AGE`) is treated as missing by `StateAssembler`. Fresh rows in
  `telemetry` but an empty depot-state view points at the read path, not ingest.
- **transaction_id mismatch** — MeterValues carry the `transaction_id` from
  StartTransaction; if the open `charging_sessions` row didn't get created
  (handler restart mid-boot), rows may not associate. Cross-restart recovery
  rehydrates open sessions on the next BootNotification (migrations 012/013).

### 3. Queue not draining (SetChargingProfile never reaches the charger)

**Symptom:** an optimization ran and wrote commands, but the charger never
receives `SetChargingProfile` (simulator exits `1` on the profile-wait timeout).

**Confirm:**

```sql
SELECT id, station_id, command_type, status, created_at, sent_at, error
FROM charging_command_queue WHERE station_id = '<sid>'
ORDER BY created_at DESC LIMIT 20;
```

`status` is one of `pending | acked | failed | expired` (the migration-013 CHECK;
the code also stamps `sent` once a push goes out). What each tells you:

- **`pending` and not moving** — the WS handler's `ChargingCommandQueueConsumer`
  isn't draining. Either the charger is offline (rows stay `pending` until the
  BootNotification replay path flushes them on reconnect — power-cycle / wait for
  reconnect), or the WS-handler service is down. Confirm the charger has a live
  WebSocket via a recent `connector_status` row that is **not** `Unavailable` /
  `ConnectionLost` (the close hook appends those when the socket drops).
- **`failed`** — the push was attempted and the charger rejected it or the RPC
  errored; read the `error` column. Common: profile shape the charger won't
  accept.
- **`expired`** — the command aged out before the charger came back. Re-run the
  optimization once the charger is reliably online.

Production note: the API process writes queue rows but does **not** push
in-process (`OCPP_SERVER_ENABLED=false`); only the WS handler drains the queue.
If the WS handler isn't running, nothing drains.

### 4. Auth failures (charger can't authenticate the WebSocket)

**Symptom:** the WebSocket is rejected at connect (HTTP 401 before any OCPP
frame); the simulator exits `3` at boot.

**Confirm:**

```sql
SELECT station_id, username, active, last_rotated_at, last_used
FROM station_credentials WHERE station_id = '<sid>';
```

**Causes & fixes:**
- **No active credential.** `verify_ocpp_basic_auth`
  (`src/security/ocpp_auth.py`) requires a row where `username = station_id`
  **and** `active = TRUE`, and bcrypt-verifies the password. A missing row, an
  inactive row, and a wrong password are intentionally indistinguishable. Mint a
  fresh credential via
  `POST /admin/depots/{id}/chargers/{cid}/rotate_credentials` and reconfigure the
  charger with the plaintext (returned **exactly once**).
- **Charger configured with stale credentials** — after any rotation the
  physical charger must be updated; the old password stops working immediately.
- **Wrong username** — the Basic Auth username must equal the `station_id`, not a
  display name.

---

## Manual operator override

If a driver is blocked at the charger and you need to authorize a charge out of
band, use the operator override:

```
POST /admin/depots/{id}/chargers/{cid}/manual_authorize
```

Inspect the last override with
`GET /admin/depots/{id}/chargers/{cid}/last_manual_override`. See
`docs/frontend/manual_charger_authorize.md` for the operator-facing flow.

---

## Rollback

Roll back the riskiest behaviors without redeploying, in increasing order of
blast radius:

1. **Disable local-auth-list push** — `OCPP_DISABLE_LOCAL_AUTH_LIST=true`, restart
   the WS handler. Chargers fall back to central online Authorize. This is the
   first thing to try for boot/reconnect problems.
2. **Disable optimization** — set `OPTIMIZATION_ENABLED=false`. No new
   `SetChargingProfile` rows are written; chargers run at their own default rate.
   In-flight queued commands still drain unless you also stop the WS handler.
3. **Stop dispatch entirely** — stop the WS Handler service. Queue rows accumulate
   as `pending` and replay on the charger's next BootNotification once you bring
   it back, so no commands are lost.
4. **Deploy rollback** — redeploy the previous known-good image of both services.
   Migrations are idempotent and re-run on deploy, so a code rollback does not
   require a schema rollback.

Credential incidents: rotate via the `rotate_credentials` endpoint (invalidates
the old password immediately). Rotating `CHARGER_LOG_UPLOAD_SIGNING_KEY`
invalidates every in-flight diagnostic-upload URL.

---

## Escalation

When triage doesn't resolve it within the pilot window, gather and escalate:

- **What** — the failing stage (simulator exit code or the lifecycle step that
  stalls) and the charger vendor/model/firmware (from BootNotification /
  `charging_stations.vendor` + `firmware_version`).
- **Service health** — `GET /health` output, and whether the WS Handler service
  is running.
- **DB evidence** — the relevant rows from the triage queries above:
  `connector_status`, `charging_command_queue`, `charging_sessions`, `telemetry`,
  `station_credentials` (never include `password_hash`), and `security_events`.
- **Logs** — WS-handler logs around the failure window (boot exchange,
  `local_auth_sync station=<sid> ...` lines, queue-consumer lines).

Escalation order: on-call backend engineer → platform owner. For suspected
charger-firmware bugs (e.g. ABB Terra AC `SendLocalList` behavior), capture a
charger-side diagnostic log via
`POST /admin/depots/{id}/chargers/{cid}/sessions/{sid}/fetch_logs` and attach the
parsed comparison from
`GET /admin/depots/{id}/sessions/{sid}/log_comparison`.

---

## Reference

- `scripts/preflight.sh` — automated pre-flight checks.
- `scripts/pilot_simulator.py` — standalone OCPP 1.6 lifecycle simulator.
- `docs/PRD_Depot_Agent.md` — product spec.
- `docs/plans/ocpp_local_auth_list_roadmap.md` — local-auth-list phase status.
- `docs/EVEREST_TESTING.md` — closed-loop EVerest smoke test.
