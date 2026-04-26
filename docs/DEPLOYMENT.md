# Favonius Energy Platform - Deployment Guide

## Overview

This guide covers **trial deployment on Railway** with two services: **Main API Backend** (REST + optimization) and **WebSocket Handler** (OCPP, VDV 463, BACnet/SC). Time-series data uses an **external TimescaleDB provider** (no database service on Railway required).

Kubernetes-based deployment has been removed for the trial and can be reintroduced later if needed.

**Reference:** PRD Section 5.2 (Architecture), Section 10 (Non-Functional Requirements)

## Railway Deployment

Deploy **two services** from this repo on [Railway](https://railway.com): **Main API** (REST + optimization) and **WebSocket Handler** (OCPP, VDV 463, BACnet/SC). Use an **external TimescaleDB provider** for time-series data; do not use the default Railway PostgreSQL (app requires TimescaleDB).

### Project layout (two services)

| Service | Role | Start command |
|---------|------|----------------|
| **API** | FastAPI REST, optimization, `/health` | `uvicorn src.api.main:app --host 0.0.0.0 --port $PORT` |
| **WebSocket Handler** | OCPP, VDV 463, BACnet/SC, telemetry to TimescaleDB | `python -m src.websocket_handler.main` |

Both services use the same repo and same Dockerfile. Configure each service in the Railway dashboard (root directory, Dockerfile build). API service can use `railway.json` for config-as-code; WebSocket Handler is configured via dashboard (start command, variables, no healthcheck path).

### Database: external TimescaleDB

- Set **API service** `DATABASE_URL` to your external TimescaleDB connection string (e.g. Timescale Cloud). Mark as **Secret**.
- Set **WebSocket Handler** Timescale env vars from the same provider: `TIMESCALE_SERVICE_URL` or `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE` (see table below). Mark credentials as **Secret**.

### API service: variables and secrets

| Variable | Required | Secret? | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | Yes | External TimescaleDB URL |
| `JWT_SECRET_KEY` | Yes | Yes | e.g. `openssl rand -hex 32` |
| `OCPP_SERVER_ENABLED` | No | No | `false` when using separate WebSocket Handler |
| `OCPP_USE_SAME_PORT` | No | No | `false` (OCPP is on WebSocket Handler service) |
| `CORS_ORIGINS` | Recommended | No | Production frontend origin(s) |
| `ENVIRONMENT` | No | No | `production` |
| `OPTIMIZATION_TIMEOUT` | No | No | Default 60 |
| `OPTIMIZATION_MIP_GAP` | No | No | Default 0.01 |
| `GUROBI_LIC_CONTENT` | If using Gurobi | Yes | License file contents; write to `/opt/gurobi/gurobi.lic` via entrypoint if needed |

### WebSocket Handler service: variables and secrets

| Variable | Required | Secret? | Description |
|----------|----------|---------|-------------|
| `WEBSOCKET_PORT` | No | No | Optional: set to `$PORT` or leave unset; app uses Railway's `PORT` when set |
| `STRICT_STARTUP_VALIDATION` | No | No | `false` on Railway if DBs may be temporarily unavailable during boot; `true` for fail-fast behavior |
| `TIMESCALE_SERVICE_URL` or `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`/`PGSSLMODE` | Yes | Yes for credentials | Same TimescaleDB as API |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY` | Yes | Yes for keys | Supabase project |
| `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD` | Yes | Yes for password | Supabase DB connection |
| `USE_KUBERNETES_SECRETS` | No | No | `false` |
| `FALLBACK_TO_ENV` | No | No | `true` |
| `ENVIRONMENT` | No | No | `production` |

Healthcheck: do **not** set an HTTP healthcheck path for the WebSocket Handler (it is a pure WebSocket server). Optionally add a lightweight HTTP `/health` on the same port later for Railway readiness.

### WebSocket URL for hardware

- **OCPP**: `wss://<ws-service-public-domain>/ocpp/{charge_point_id}` — use subprotocol **`ocpp1.6`** (PRD: OCPP 1.6J only).
- **VDV 463**: `wss://<ws-service-public-domain>/vdv463/{presystem_id}`
- **BACnet/SC**: `wss://<ws-service-public-domain>/bacnet/{device_id}`

Generate a public domain for the WebSocket Handler service (Settings → Networking → Generate domain). Railway terminates TLS (WSS).

### Migrations

API service only: migrations run automatically before each deploy when `railway.json` includes `"preDeployCommand": "python scripts/run_migrations.py"`. Ensure `DATABASE_URL` is set before the first deploy. Migrations live in `migrations/` and are applied in filename order.

### API health check

Configure in Railway (or rely on `railway.json`): path `/health`, timeout 60 s. The API listens on `$PORT`.

### Config as code with two services from one repo

When API and simulator/WebSocket services are deployed from the **same repo**, avoid service-specific commands in root `railway.json` (they can override dashboard settings on every deploy).

- Keep `railway.json` minimal (schema only), **or** disable config-as-code on one service.
- Set each service's Dockerfile, start command, pre-deploy command, and healthcheck in the Railway dashboard.
- API should run: `uvicorn src.api.main:app --host 0.0.0.0 --port $PORT` with `/health`.
- Simulator/WebSocket service should run its own command and should not inherit API migration/health settings.

### Trial deployment checklist

1. Create a Railway project; add **two** services from the same GitHub repo (same root, same Dockerfile).
2. **API service:** Set `DATABASE_URL`, `JWT_SECRET_KEY` (secrets); set `OCPP_SERVER_ENABLED=false`, `OCPP_USE_SAME_PORT=false`, `CORS_ORIGINS`, `ENVIRONMENT=production`. Generate public domain.
3. **WebSocket Handler service:** Set `WEBSOCKET_PORT=$PORT` and all Timescale + Supabase env vars (secrets where appropriate). Set `USE_KUBERNETES_SECRETS=false`, `FALLBACK_TO_ENV=true`. Generate public domain. Do not set healthcheck path.
4. Deploy API first so pre-deploy migrations run against your external TimescaleDB.
5. Deploy WebSocket Handler.
6. Validate: `curl https://<api-domain>/health` returns 200.
7. Validate: connect to `wss://<ws-domain>/ocpp/{charge_point_id}` with subprotocol `ocpp1.6` and verify handshake/session.
8. Confirm TimescaleDB tables exist and telemetry writes (e.g. from OCPP MeterValues) succeed.

### Detailed Railway setup playbook (API + WebSocket Handler)

Use this when setting up from scratch in the Railway dashboard.

#### A) API service (FastAPI + optimization)

1. **Create service from this repo**
   - In Railway project, add a service from GitHub repo root.
   - Keep root directory as `/` and build using `Dockerfile`.

2. **Set runtime command**
   - Service start command:
     - `sh -c 'exec uvicorn src.api.main:app --host 0.0.0.0 --port "${PORT}"'`
   - Why: the Dockerfile default already runs API, but setting it explicitly avoids ambiguity when sharing one repo for multiple services.
   - If Railway shows `Failed to parse start command`, remove surrounding backticks/JSON and paste only the raw command string in the Start Command field.
   - Example (safe quoting): `sh -c 'exec uvicorn src.api.main:app --host 0.0.0.0 --port "${PORT}"'`

3. **Set API environment variables**
   - Required secrets:
     - `DATABASE_URL=<external_timescaledb_url>`
     - `JWT_SECRET_KEY=<random_secret>`
   - Recommended non-secret variables:
     - `ENVIRONMENT=production`
     - `OCPP_SERVER_ENABLED=false`
     - `OCPP_USE_SAME_PORT=false`
     - `CORS_ORIGINS=<frontend_origin_or_csv>`

4. **Configure pre-deploy migration step**
   - Pre-deploy command:
     - `python scripts/run_migrations.py`
   - Migrations should run only on API service.

5. **Configure healthcheck**
   - Healthcheck path: `/health`
   - Timeout: 60s (or Railway default if unset).
   - The API must listen on Railway-provided `$PORT`.

6. **Networking/domain**
   - Generate public domain for API service.
   - Validate API endpoint:
     - `curl https://<api-domain>/health`

7. **Deploy and validate logs**
   - Confirm in logs:
     - DB pool initializes
     - Controllers start
     - No repeated fatal startup exceptions

#### B) WebSocket Handler service (OCPP/VDV/BACnet)

1. **Create second service from same repo**
   - Add another service from the same GitHub repo/root.
   - Use same `Dockerfile`, but different runtime command and variables.

2. **Set runtime command (critical)**
   - Service start command:
     - `python -m src.websocket_handler.main`
   - Why: Dockerfile default starts API (`uvicorn ...`), not the WebSocket handler.

3. **Set WebSocket handler environment variables**
   - Core runtime:
     - `ENVIRONMENT=production`
     - `WEBSOCKET_PORT` **unset** (recommended) so the app uses Railway `PORT` automatically

   - Important:
     - Do **not** set `WEBSOCKET_PORT=$PORT` in Railway Variables. Railway stores it literally as `$PORT`, which will fail integer parsing in older versions.
     - If you set `WEBSOCKET_PORT`, set a numeric value only (for example `9000` in local/non-Railway environments).
   - If dependency validation blocks startup during platform boot, set `STRICT_STARTUP_VALIDATION=false` and rely on runtime reconnect logic.
   - Secrets/config for data backends:
     - Timescale: `TIMESCALE_SERVICE_URL` **or** `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`/`PGSSLMODE`
     - Supabase: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`
     - Supabase DB: `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD`
   - Secrets loading mode (Railway):
     - `USE_KUBERNETES_SECRETS=false`
     - `FALLBACK_TO_ENV=true`

4. **Healthcheck settings**
   - Do **not** set HTTP healthcheck path by default for this service.
   - This service is primarily WebSocket and should not inherit API `/health` checks.

5. **Networking/domain**
   - Generate public domain for WebSocket service.
   - Chargers/simulators must connect to this domain (not API domain):
     - `wss://<ws-domain>/ocpp/{charge_point_id}`
     - Subprotocol: `ocpp1.6`

6. **Deploy and validate logs**
   - Confirm startup includes configuration validation and server start.
   - During test connections, confirm `BootNotification` and subsequent OCPP exchanges are received.

#### C) Cross-service sanity checks (recommended order)

1. Deploy API service first (runs migrations).
2. Deploy WebSocket Handler second.
3. Verify API health endpoint returns 200.
4. Verify WebSocket handshake against WS domain with `ocpp1.6`.
5. Verify telemetry rows arrive in Timescale after MeterValues.
6. Verify no repeated `BootNotification` timeout/retry loops from simulator.
7. Trigger an optimization (`POST /optimize` on the API service) and confirm:
   * a row lands in `charging_command_queue` (status `pending`),
   * the WebSocket handler's `ChargingCommandQueueConsumer` flips it to `sent`
     within ~2 s if the matching charger is connected, and
   * `GET /admin/ocpp/{cp_id}/state` (Bearer token w/ owner role) returns the
     full dump for that charger.

#### Queue-mediated SetChargingProfile dispatch (sessions 2–3)

Production runs the API service with `OCPP_SERVER_ENABLED=false`, so the
optimizer cannot push `SetChargingProfile` directly to a charger socket.
Instead, after every optimization run the API enqueues one row per
scheduled vehicle in `charging_command_queue` (migration 013, with the
`pg_notify` trigger from migration 014). The WebSocket Handler service
runs `ChargingCommandQueueConsumer`, which:

* polls `charging_command_queue` every 2 s (and listens for `pg_notify`
  on channel `charging_command_queue` for sub-second wake-up),
* calls `send_charging_profile` on the in-memory `OCPP16Session` for the
  target `charge_point_id`,
* marks the row `sent` (charger Accepted), `failed` (charger Rejected /
  push raised) or leaves it `pending` if the charger is offline, and
* publishes `profile_push_latency_seconds{station_id, outcome}`.

Rows that stay `pending` are flushed by the BootNotification replay path
(`OCPP16Session._on_boot` → `replay_queued_commands`) when the charger
reconnects. Expired rows (>60 min by default) move to `expired` so the
backlog does not grow unbounded.

#### D) Pilot-debug endpoints + metrics

Both services expose `/metrics` (Prometheus text). Session 3 added:

| Metric | Type | Labels | Source |
|---|---|---|---|
| `profile_push_latency_seconds` | Histogram | `station_id`, `outcome` | queue consumer + `OCPP16Session.send_charging_profile` |
| `db_write_latency_seconds` | Histogram | `table` | telemetry batch flush + `connector_status` writes |
| `active_transactions` | Gauge | `station_id` | inline on Start/StopTransaction; reconciled from DB every 30 s |
| `charging_command_queue_depth` | Gauge | `status` | sampled every 10 s |

The WebSocket Handler also serves `GET /admin/ocpp/{cp_id}/state` on the
API port (default 8080). Owner JWT required; returns connected/vendor/
model/last\_boot\_at/last\_heartbeat\_at, latest `connector_status` per
connector, open transactions, and a `charging_command_queue` rollup.
Use it to triage individual chargers without spelunking through the DB.

#### E) Common misconfigurations to avoid

- Running only one Railway service with Dockerfile default command (API only) and expecting OCPP WS handler to be active.
- Pointing chargers/simulator to API domain instead of WebSocket domain.
- Reusing API healthcheck config on WebSocket service.
- Forgetting to disable Kubernetes secret mode on Railway (`USE_KUBERNETES_SECRETS=false`).
- Omitting Supabase/Timescale variables for WebSocket service while API variables are present.

### Updated advice (Railway docs + MCP)

**Railway MCP:** The Railway MCP tools depend on the [Railway CLI](https://docs.railway.com/guides/cli) being installed and authenticated (`railway login`). If the CLI is not available in the environment (e.g. Cursor’s backend), `check-railway-status`, `list-projects`, `list-services`, and `list-variables` will fail with “railway: command not found”. To use the MCP against your project: install the CLI, run `railway login`, and in this repo run `railway link` to link the project; then the MCP can list services/variables and help with deploys from a session where the CLI is on PATH.

**Database options:**

- **External TimescaleDB (recommended if you already have one):** Set `DATABASE_URL` on the API service to your provider’s connection string (e.g. Timescale Cloud). Mark as **Secret** in Railway. No DB service needed on Railway; time-series stays in TimescaleDB from day one.
- **Railway TimescaleDB:** Use the [TimescaleDB + PostGIS template](https://railway.com/template/timescaledb-postgis) (not default PostgreSQL). Set `DATABASE_URL` = `${{TimescaleDB.DATABASE_URL}}` (use the actual service name). For external references you must use the service name: `${{ServiceName.DATABASE_URL}}`; a bare `${{DATABASE_URL}}` is empty.

**Secrets to set (mark as Secret in Railway UI):**

| Variable | Required | Secret? | Notes |
|----------|----------|---------|--------|
| `DATABASE_URL` | Yes | Yes | Full URL from your TimescaleDB provider or `${{TimescaleDB.DATABASE_URL}}` |
| `JWT_SECRET_KEY` | Yes | Yes | e.g. `openssl rand -hex 32` |
| `GUROBI_LIC_CONTENT` | If using Gurobi | Yes | Raw contents of `gurobi.lic`; app/entrypoint must write to `/opt/gurobi/gurobi.lic` |

**Recommended (non-secret):** For two-service trial: API uses `OCPP_USE_SAME_PORT=false`, `OCPP_SERVER_ENABLED=false`; set `CORS_ORIGINS`, `ENVIRONMENT=production`. Optional: `HANDOFF_DEST_DEPOT_ENDPOINT`, `DEFAULT_DEPOT_ENDPOINT` for inter-depot handoff.

**GitHub deploy:** In Service Settings, set the branch that triggers deploys. To wait for GitHub Actions before deploying, enable **Wait for CI** (requires a workflow that runs on push); failed workflows cause the deploy to be skipped.

**Healthchecks (from Railway docs):** Railway calls the health path until HTTP 200. Requests come from hostname `healthcheck.railway.app`; if your app restricts by host, allow that host. Default timeout is **300 seconds**; `railway.json` overrides to 60s. The app must listen on the injected `PORT`. To override timeout in the dashboard use variable `RAILWAY_HEALTHCHECK_TIMEOUT_SEC`. Healthchecks run only at deploy time (not continuous monitoring).

**Config as code:** Settings in `railway.json` override the dashboard for each deployment; the dashboard is not updated. Deployment details in the Railway UI show which values came from the config file.

**WebSockets:** With two services, OCPP/VDV/BACnet connect to the **WebSocket Handler** service domain: `wss://<ws-service-domain>/ocpp/{charge_point_id}` (subprotocol `ocpp1.6`). Railway terminates TLS (WSS).

## Additional Resources

- [PRD_v2_7_Building_Integration.md](PRD_v2_7_Building_Integration.md) - Product Requirements Document
- [favonius_development_plan_v3.md](../favonius_development_plan_v3.md) - Development Plan
