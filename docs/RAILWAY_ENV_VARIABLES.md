# Railway environment variables – full guide

Where to set each variable: **Railway project → select service (API or WebSocket Handler) → Variables tab.** Add each name/value; use **Encrypt** (Secret) for credentials.

---

## API service

### Required (must set)

| Variable | Secret? | Where / how to get it |
|----------|---------|------------------------|
| **DATABASE_URL** | Yes | **Full PostgreSQL connection string** for your single database (TimescaleDB, Supabase Postgres, or TigerDB). Format: `postgresql://USER:PASSWORD@HOST:PORT/DATABASE` or with SSL: `postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require`. **Supabase:** Project Settings → Database → Connection string → “URI” (use Session mode, port 5432, or Transaction mode, port 6543 — copy the URI as-is). **Timescale Cloud:** Service → Connection info → copy the connection string. **TigerDB / other:** From your provider’s dashboard or docs. This URL is used for the app and for pre-deploy migrations (`scripts/run_migrations.py`). |
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

### Pre-deploy command (API only)

- In Railway: **Settings** (or **Deploy**) for the **API** service, set **Pre-deploy command** to:  
  `python scripts/run_migrations.py`  
- Migrations use `DATABASE_URL` (or `TIMESCALE_SERVICE_URL` if set). They create tables on first deploy; ensure `DATABASE_URL` is set before the first deploy.

---

## WebSocket Handler service

The WebSocket handler currently reads **Timescale** from `TIMESCALE_SERVICE_URL` or from `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`/`PGSSLMODE`. It does **not** yet read `DATABASE_URL`; once the planned change is implemented, you can set **only** `DATABASE_URL` (same as API) for the database. Until then, use either a full URL in `TIMESCALE_SERVICE_URL` or the individual PG vars below.

### Required – database (Timescale / Postgres)

Use **one** of these two options.

**Option A – single URL (preferred once code supports it)**

| Variable | Secret? | Where / how to get it |
|----------|---------|------------------------|
| **DATABASE_URL** | Yes | *(After the planned change: same value as API’s `DATABASE_URL`. Until then, prefer Option B.)* |
| **TIMESCALE_SERVICE_URL** | Yes | **Full PostgreSQL URL** for the **same** database as the API. Same source as API’s `DATABASE_URL`: Supabase (Settings → Database → URI), Timescale Cloud (Connection info), or TigerDB dashboard. Example: `postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require`. |

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

## Single database (Supabase or TigerDB) – summary

If **one** Postgres is used for both API and WebSocket (e.g. Supabase or TigerDB):

1. **API:** Set `DATABASE_URL` to that database’s **full URI** (from Supabase Database → URI or TigerDB connection string). Set `JWT_SECRET_KEY` (e.g. Supabase JWT secret from API settings). Run migrations via pre-deploy so tables exist.
2. **WebSocket Handler:** Set **Timescale** to the **same** database: either `TIMESCALE_SERVICE_URL` = same URI as API’s `DATABASE_URL`, or `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` (and `PGSSLMODE`) matching that DB. Set all **SUPABASE_*** vars from the **same** Supabase project (if you use Supabase); for Supabase-as-DB, `SUPABASE_DB_*` and Timescale point to the same Postgres instance.
3. **“Tenant or user not found”** on Supabase: Fix `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_USER`, and `SUPABASE_DB_PASSWORD` using the exact values from Supabase Dashboard → Database → Connection string for the mode (Session vs Transaction) you use.

---

## Quick checklist

**API service**

- [ ] `DATABASE_URL` (Secret) = full Postgres URI
- [ ] `JWT_SECRET_KEY` (Secret) = e.g. `openssl rand -hex 32` or Supabase JWT secret
- [ ] `ENVIRONMENT=production`
- [ ] `OCPP_SERVER_ENABLED=false`
- [ ] `OCPP_USE_SAME_PORT=false`
- [ ] `CORS_ORIGINS` = your frontend origin(s)
- [ ] Pre-deploy: `python scripts/run_migrations.py`

**WebSocket Handler service**

- [ ] `TIMESCALE_SERVICE_URL` (Secret) = same DB as API **or** `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`/`PGSSLMODE`
- [ ] `SUPABASE_URL`, `SUPABASE_ANON_KEY` (Secret), `SUPABASE_SERVICE_KEY` (Secret)
- [ ] `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD` (Secret) from same Supabase project
- [ ] `USE_KUBERNETES_SECRETS=false`, `FALLBACK_TO_ENV=true`
- [ ] `ENVIRONMENT=production`
- [ ] `WEBSOCKET_PORT` unset (so Railway `PORT` is used) or set to a number
