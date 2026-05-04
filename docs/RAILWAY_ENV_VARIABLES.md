# Railway environment variables – full guide

Where to set each variable: **Railway project → select service (API or WebSocket Handler) → Variables tab.** Add each name/value; use **Encrypt** (Secret) for credentials.

---

## API service

### Required (must set)

| Variable | Secret? | Where / how to get it |
|----------|---------|------------------------|
| **DATABASE_URL** | Yes | **Supabase Postgres URI** (static / reference data: `sites`, `vehicles`, `charging_stations`, tenant mirror, …). Project Settings → Database → Connection string → “URI” (Session/direct port **5432** or pooler **6543** — copy as-is). **Do not** point this at TigerCloud in production. |
| **TIMESCALE_SERVICE_URL** | Yes | **TigerCloud / favonius-timeseries** (or other TimescaleDB) **full URI** for telemetry, sessions, prices, `connector_status`, `optimization_runs`, audit/rate-limit tables, etc. Format: `postgres://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require`. **Required** when `ENVIRONMENT` is `production` or `staging` — the API refuses to start without it so the time-series pool never accidentally uses `DATABASE_URL`. |
| **JWT_SECRET_KEY** | Yes | **Secret used to verify JWTs** (e.g. Supabase-issued tokens). Generate a random value, e.g. run in terminal: `openssl rand -hex 32`. Use the same secret as your auth provider (e.g. Supabase JWT secret from Project Settings → API → “JWT Secret”); if your frontend uses Supabase Auth, use Supabase’s JWT secret here so the API accepts those tokens. |

### Recommended (non-secret)

| Variable | Example | Where / how to get it |
|----------|---------|------------------------|
| **ENVIRONMENT** | `production` | Set to `production` on Railway so the app uses production behaviour (e.g. no debug defaults). |
| **OCPP_SERVER_ENABLED** | `false` | Set to `false` when you run a **separate WebSocket Handler** service for OCPP. The API then only serves REST and does not start an OCPP server. |
| **OCPP_USE_SAME_PORT** | `false` | Set to `false` when OCPP is on the WebSocket Handler service. |
| **CORS_ORIGINS** | `https://your-app.vercel.app` or `https://app.example.com` | Comma-separated list of allowed frontend origins (no trailing slash). Get from your frontend’s deployed URL(s). Use `*` only for development. |

### Optional (API)

| Variable | Default | Where / how to get it |
|----------|---------|------------------------|
| **OPTIMIZATION_TIMEOUT** | `60` | Solver time limit in seconds (PRD: &lt; 60 s). |
| **OPTIMIZATION_MIP_GAP** | `0.01` | MIP optimality gap (e.g. 0.01 = 1%). |
| **JWT_ALGORITHM** | `HS256` | Only change if your auth provider uses another algorithm. |
| **GUROBI_LIC_CONTENT** | — | **Secret.** Required only if you use Gurobi. Paste the **entire contents** of your `gurobi.lic` file. The app/entrypoint must write this to `/opt/gurobi/gurobi.lic`; if your Dockerfile/entrypoint does not do that, you may need to add it. Without this, the solver falls back to HiGHS. |
| **HANDOFF_DEST_DEPOT_ENDPOINT** | — | Base URL of the “destination” depot API for inter-depot handoff (e.g. `https://other-api.railway.app`). Only if you use handoff. |
| **DEFAULT_DEPOT_ENDPOINT** | `http://localhost:8000` | Default depot API URL used when a specific destination is not set. |
| **MAXMIND_LICENSE_KEY** | — | **Secret.** MaxMind license key for the GeoLite2-Country DB used by the Article 73-3 geo-blocking middleware. **Set in two places:** (1) under **Build** variables so the Dockerfile downloads the DB at image build (`Dockerfile:54-97`); (2) under **Service** variables so `src/security/geo_block.py::_download_geoip_db` can re-download with retries at startup if the build-time download was skipped or hit a transient MaxMind outage. Without it, the service starts with no GeoIP DB and fails closed (every non-private IP gets a 403 “Access denied”). Get a key for free from maxmind.com → Account → License Keys. |

### Pre-deploy command (API only)

- In Railway: **Settings** (or **Deploy**) for the **API** service, set **Pre-deploy command** to:  
  `python scripts/run_migrations.py --target ts`  
- This runs numbered SQL under `migrations/` against **TigerCloud** using `TIMESCALE_SERVICE_URL`. In production, `DATABASE_URL` points at Supabase, so the pre-deploy **must** have `TIMESCALE_SERVICE_URL` set (the runner does not fall back to `DATABASE_URL` for `--target ts` in that layout). For local single-DB dev, `TIMESCALE_SERVICE_URL` may be unset and the runner falls back to `DATABASE_URL`. Supabase static schema: `python scripts/run_migrations.py --target supabase` with `DATABASE_URL` = Supabase.

**Post-deploy (optional, read-only):** with the same `TIMESCALE_SERVICE_URL` as the service, run `python scripts/verify_timescale_schema.py` to confirm `charging_sessions` live columns, `connector_status`, and `telemetry` match what `GET /depots/{id}/chargers` expects. If it fails, re-run the Timescale pre-deploy (additive migrations only).

---

## WebSocket Handler service

The WebSocket handler reads **Timescale** from `TIMESCALE_SERVICE_URL` (preferred: a single URI; host/user/password/port/database are parsed from it). Optional `PGHOST`/`PGUSER`/… override pieces of the URI. In **production** or **staging**, `TIMESCALE_SERVICE_URL` is **required** (same TigerCloud URI as the API). In local development only, if `TIMESCALE_SERVICE_URL` is unset, it may fall back to `DATABASE_URL` when that value is a `postgres://` URI (single-DB docker-compose).

### Required – database (Timescale / Postgres)

Use **one** of these two options.

**Option A – single URL (preferred)**

| Variable | Secret? | Where / how to get it |
|----------|---------|------------------------|
| **TIMESCALE_SERVICE_URL** | Yes | **Same TigerCloud / Timescale URI as the API** (`favonius-timeseries`). Example: `postgres://USER:PASSWORD@HOST:PORT/tsdb?sslmode=require`. |

**Option B – individual PG vars (use if not using a single URL)**

| Variable | Secret? | Where / how to get it |
|----------|---------|------------------------|
| **PGHOST** | No | Database host from your provider (e.g. `db.xxxx.supabase.co` or `xxxx.pooler.supabase.com`, or Timescale Cloud host). Same DB as API. |
| **PGPORT** | No | `5432` (Session/direct) or `6543` (Supabase Transaction pooler). |
| **PGDATABASE** | No | Database name, usually `postgres` for Supabase. |
| **PGUSER** | No | DB user (e.g. `postgres` or Supabase pooler user like `postgres.projectref`). |
| **PGPASSWORD** | Yes | DB password from Supabase (Settings → Database → Database password) or your provider. |
| **PGSSLMODE** | No | `require` for cloud DBs; `disable` only for local dev. |

### Required – Supabase (REST API / auth / sync)

These are for the Supabase **client** (REST API, auth, optional sync). Get them from **one** Supabase project.

| Variable | Secret? | Where / how to get it |
|----------|---------|------------------------|
| **SUPABASE_URL** | No (but sensitive) | Supabase project URL. **Supabase Dashboard** → Project Settings → API → “Project URL” (e.g. `https://xxxx.supabase.co`). |
| **SUPABASE_ANON_KEY** | Yes | **Supabase Dashboard** → Project Settings → API → “Project API keys” → `anon` `public` key. Long JWT-like string. |
| **SUPABASE_SERVICE_KEY** | Yes | Same page → `service_role` `secret` key. Never expose in frontend; backend only. |
| **SUPABASE_DB_HOST** | No | **Supabase Dashboard** → Project Settings → **Database** → “Connection string” → **Host**. For **Session mode** (recommended for long-lived apps): use “Direct connection” host, e.g. `db.<project-ref>.supabase.co`. For **Transaction mode**: use “Connection pooling” host, e.g. `aws-0-<region>.pooler.supabase.com`, and port **6543**. If you get “Tenant or user not found”, the host/project ref or pooler mode is wrong — double-check project and use the exact host from the chosen connection mode. |
| **SUPABASE_DB_PORT** | No | From same Database section: **5432** for Session/direct, **6543** for Transaction pooler. |
| **SUPABASE_DB_NAME** | No | Usually `postgres` (Supabase default). Shown in Database connection string. |
| **SUPABASE_DB_USER** | No | From Database connection string. Direct: often `postgres`. Pooler: often `postgres.<project-ref>`. |
| **SUPABASE_DB_PASSWORD** | Yes | **Supabase Dashboard** → Project Settings → Database → “Database password” (the one you set for the project). Use the same password as in your `DATABASE_URL` / Timescale URL if you use Supabase as the single DB. |

### Recommended – WebSocket Handler

| Variable | Example | Where / how to get it |
|----------|---------|------------------------|
| **ENVIRONMENT** | `production` | Set to `production`. |
| **WEBSOCKET_PORT** | *(leave unset on Railway)* | Railway injects `PORT`; the app reads `PORT` when `WEBSOCKET_PORT` is unset. If you set it explicitly on Railway, use a **number** (e.g. `8080`), not `$PORT` — some versions don’t expand `$PORT` in variables. |
| **USE_KUBERNETES_SECRETS** | `false` | Set to `false` on Railway so the app reads from normal env vars, not Kubernetes secrets. |
| **FALLBACK_TO_ENV** | `true` | Set to `true` so the app uses environment variables when no secrets backend is configured. |
| **STRICT_STARTUP_VALIDATION** | `false` | Optional. Set to `false` if you want the service to start even when DB validation fails at boot (e.g. transient network issues). Validation failures are still logged; the app may fail later when using DB. Prefer fixing DB credentials and using `true` once stable. |

### Optional – WebSocket Handler

| Variable | Default | Where / how to get it |
|----------|---------|------------------------|
| **LOG_LEVEL** | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| **METRICS_PORT** | `8080` | Port for Prometheus metrics (if exposed). |
| **HEALTH_CHECK_PORT** | `8081` | Port for internal health checks. |
| **PRICE_FEEDER_ENABLED** | `true` | Set to `false` to disable CAISO/ENTSO-E price ingestion in the WebSocket service. |
| **PRICE_FEEDER_NODES** | `TH_SP15_GEN-APND,TH_NP15_GEN-APND` | CAISO node IDs; only if you use CAISO and want to override. |
| **PRICE_FEEDER_ENTSOE_ZONES** | — | Comma-separated EIC codes for ENTSO-E (e.g. `10Y1001A1001A82H` for DE-LU). Only for European depots. |
| **EUROPEAN_ELECTRICITY_API** | — | ENTSO-E API token if you use European price feeds. |
| **OPTIMIZATION_ENABLED** | `true` | Set to `false` to disable optimization in the WebSocket handler. |
| **VDV463_ENABLED** | `true` | Set to `false` if you don’t use VDV 463. |
| **SUPABASE_MAX_CONNECTIONS** | `20` | Max connections to Supabase DB. |
| **SUPABASE_CONNECTION_TIMEOUT** | `30` | Connection timeout in seconds. |
| **SUPABASE_ENABLE_REALTIME** | `true` | Enable Supabase Realtime subscriptions. |
| **TIMESCALE_MAX_CONNECTIONS** | `100` | Max Timescale/Postgres connections from this service. |
| **TIMESCALE_POOL_SIZE** | `20` | Connection pool size. |

---

## Production split (Supabase static + TigerCloud timeseries) – summary

1. **API:** Set `DATABASE_URL` to **Supabase** (static schema). Set `TIMESCALE_SERVICE_URL` to **TigerCloud** (`favonius-timeseries`). Set `JWT_SECRET_KEY` / JWKS vars as for Supabase Auth. Pre-deploy: `python scripts/run_migrations.py --target ts` so operational tables exist on TigerCloud.
2. **WebSocket Handler:** Set `TIMESCALE_SERVICE_URL` to the **same** TigerCloud URI as the API. Set all **SUPABASE_*** vars from the Supabase project (REST + optional direct DB sync). `SUPABASE_DB_*` targets Supabase Postgres, not TigerCloud.
3. **“Tenant or user not found”** on Supabase: Fix `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_USER`, and `SUPABASE_DB_PASSWORD` using the exact values from Supabase Dashboard → Database → Connection string for the mode (Session vs Transaction) you use.

### Local single-database (optional)

If **one** Postgres hosts both roles (e.g. local docker-compose), you may set only `DATABASE_URL` and leave `TIMESCALE_SERVICE_URL` unset with `ENVIRONMENT=development`; the API uses `DATABASE_URL` for both pools. This layout is **not** supported for `production` / `staging`.

---

## Quick checklist

**API service**

- [ ] `DATABASE_URL` (Secret) = Supabase Postgres URI
- [ ] `TIMESCALE_SERVICE_URL` (Secret) = TigerCloud / Timescale URI
- [ ] `JWT_SECRET_KEY` (Secret) = e.g. `openssl rand -hex 32` or Supabase JWT secret
- [ ] `ENVIRONMENT=production`
- [ ] `OCPP_SERVER_ENABLED=false`
- [ ] `OCPP_USE_SAME_PORT=false`
- [ ] `CORS_ORIGINS` = your frontend origin(s)
- [ ] Pre-deploy: `python scripts/run_migrations.py --target ts`

**WebSocket Handler service**

- [ ] `TIMESCALE_SERVICE_URL` (Secret) = same DB as API **or** `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`/`PGSSLMODE`
- [ ] `SUPABASE_URL`, `SUPABASE_ANON_KEY` (Secret), `SUPABASE_SERVICE_KEY` (Secret)
- [ ] `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD` (Secret) from same Supabase project
- [ ] `USE_KUBERNETES_SECRETS=false`, `FALLBACK_TO_ENV=true`
- [ ] `ENVIRONMENT=production`
- [ ] `WEBSOCKET_PORT` unset (so Railway `PORT` is used) or set to a number

---

## Monday pilot lock-in

Set these explicitly before the Sunday-night cross-network test:

**API service**
- `OCPP_SERVER_ENABLED=false`
- `OCPP_USE_SAME_PORT=false`

**WebSocket Handler service**
- `OCPP_REQUIRE_AUTH=true`
- `WEBSOCKET_PING_INTERVAL=45`
- `WEBSOCKET_PING_TIMEOUT=30`
- `OCPP_DISABLE_LOCAL_AUTH_LIST=true`
