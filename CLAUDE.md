# CLAUDE.md — Favonius Energy Backend

This file provides guidance for AI assistants working in this codebase.

---

## Engineering Preferences

These preferences govern how all work in this repo should be approached. Apply them when reviewing plans, writing code, and making recommendations.

- **When resolving user requests** — Always offer **three options** to address the request (or fewer only if the problem admits fewer). For each option give **pros and cons**. **Recommend one option** and state **why** (e.g. robustness, maintainability, least change, or fit with project preferences). This gives the user a clear choice and a justified default.
- **DRY is important** — flag repetition aggressively. If the same logic appears twice, it should be extracted.
- **Well-tested code is non-negotiable** — err toward more tests, not fewer. Cover happy paths, sad paths, and edge cases.
- **"Engineered enough"** — avoid both under-engineering (fragile, hacky, no error handling) and over-engineering (premature abstraction, unnecessary complexity, features no one asked for).
- **Handle edge cases thoughtfully** — thoughtfulness > speed. Missing edge cases are bugs waiting to happen.
- **Explicit over clever** — readable code beats smart code. Future readers (and AI assistants) should not need to reverse-engineer intent.

---

## Plan Mode Protocol

When in plan mode, always follow this workflow before making any code changes.

### Before starting: ask scope

Always ask the user to choose one of:

1. **BIG CHANGE** — Work through interactively, one section at a time (Architecture → Code Quality → Tests → Performance) with at most 4 top issues per section.
2. **SMALL CHANGE** — Work through interactively with ONE question per review section.

### Review sections

For each section, output the issues with pros/cons AND an opinionated recommendation, then use `AskUserQuestion` before proceeding to the next section.

**1. Architecture review**
- Overall system design and component boundaries
- Dependency graph and coupling concerns
- Data flow patterns and potential bottlenecks
- Scaling characteristics and single points of failure
- Security architecture (auth, data access, API boundaries)

**2. Code quality review**
- Code organization and module structure
- DRY violations — be aggressive
- Error-handling patterns and missing edge cases (call these out explicitly)
- Technical debt hotspots
- Areas that are over-engineered or under-engineered

**3. Test review**
- Test coverage gaps (unit, integration, e2e)
- Test quality and assertion strength
- Missing edge case coverage
- Untested failure modes and error paths

**4. Performance review**
- N+1 queries and database access patterns
- Memory usage concerns
- Caching opportunities
- Slow or high-complexity code paths

### For each issue found

For every specific issue (bug, smell, design concern, risk):
- Describe the problem concretely, with `file_path:line_number` references
- Present 2–3 options, including "do nothing" where reasonable
- For each option: implementation effort, risk, impact on other code, maintenance burden
- Give an opinionated recommendation mapped to the engineering preferences above
- **Number issues** (1, 2, 3…) and **letter options** (A, B, C) so they're unambiguous in `AskUserQuestion`
- Put the recommended option first
- Ask for explicit agreement before proceeding

### Interaction rules
- Do not assume priorities on timeline or scale — ask
- After each review section, pause and ask for feedback before moving on
- Never skip the `AskUserQuestion` step; always wait for direction

---

## Project Overview

**Favonius Energy** is an EV Fleet Depot Optimization Platform. It coordinates EV charging schedules, stationary batteries, and building loads to reduce electricity costs by 30–50% for commercial fleet operators.

**Core capabilities:**
- OCPP 1.6/2.0.1 protocol for charger communication
- MILP-based optimization (Pyomo + Gurobi primary, HiGHS fallback)
- VDV 463 transit operations integration (BMS/ITCS interface)
- CAISO / ENTSO-E electricity price ingestion
- Gaussian Process surrogate model for energy consumption prediction
- TimescaleDB for time-series data storage
- Prometheus/Grafana observability

**Authoritative spec:** `docs/PRD_v2_7_Building_Integration.md` — always consult it for acceptance criteria, data models, and feature requirements.

---

## Repository Structure

```
Favonius_Backend/
├── src/                         # Primary application code (new architecture)
│   ├── api/
│   │   └── main.py              # FastAPI app, all REST endpoints, OCPP WebSocket mount
│   ├── core/
│   │   ├── controller.py        # DepotController — main control loop
│   │   ├── controller_config.py # ControllerConfig dataclass
│   │   ├── controller_manager.py# ControllerManager — manages per-depot controllers
│   │   ├── models.py            # Shared Python dataclasses (Depot, Vehicle, etc.)
│   │   ├── optimizer/
│   │   │   ├── milp_model.py    # Pyomo MILP model construction
│   │   │   ├── solver.py        # Gurobi + HiGHS fallback solver wrapper
│   │   │   ├── allocator.py     # Post-solve schedule allocation
│   │   │   ├── warm_start.py    # Warm-start from prior solutions
│   │   │   └── exceptions.py    # SolverError, InfeasibleModelError, SolverTimeoutError
│   │   ├── state/
│   │   │   ├── assembler.py     # StateAssembler — assembles depot state from DB
│   │   │   └── triggers.py      # TriggerMonitor, TriggerConfig — re-optimization triggers
│   │   └── surrogate/
│   │       ├── energy_model.py  # Gaussian Process energy consumption model
│   │       └── training.py      # Model training pipeline
│   ├── adapters/
│   │   ├── ocpp/
│   │   │   ├── server.py        # OCPPServer — WebSocket server for chargers
│   │   │   ├── charge_point.py  # FleetChargePoint — per-charger OCPP handler
│   │   │   ├── dispatch.py      # SetChargingProfile command dispatch
│   │   │   ├── mapping.py       # ocpp_id ↔ charger_id lookup
│   │   │   ├── telemetry.py     # MeterValues → telemetry DB writes
│   │   │   └── asgi_adapter.py  # Starlette/FastAPI WebSocket adapter
│   │   ├── vdv463/
│   │   │   ├── handler.py       # VDV 463 WebSocket handler
│   │   │   ├── messages.py      # Message parsing, validation, builders
│   │   │   ├── repository.py    # DB queries for VDV 463 state
│   │   │   ├── depot_state.py   # Depot charging info for VDV responses
│   │   │   ├── vehicle_resolver.py
│   │   │   └── charging_point_resolver.py
│   │   ├── caiso/               # CAISO price ingestion
│   │   ├── entsoe/              # ENTSO-E European price ingestion
│   │   ├── weather/             # OpenMeteo weather adapter
│   │   └── handoff/
│   │       └── manager.py       # Inter-depot vehicle handoff manager
│   ├── db/
│   │   ├── models.py            # SQLAlchemy ORM models (mirror of SQL schema)
│   │   └── queries.py           # Async DB query helpers
│   ├── monitoring/
│   │   └── metrics.py           # Prometheus metrics definitions
│   └── security/
│       ├── auth.py              # JWT token verification (Supabase)
│       ├── rate_limiter.py      # Rate limiter (optimize: 10/min, API: 100/min, handoff: 50/hr)
│       ├── validators.py        # UUID and input validators
│       ├── data_freshness.py    # Stale data detection
│       └── secrets.py           # Secrets management
│
├── src/websocket_handler/       # Legacy OCPP WebSocket service (standalone)
│   ├── main.py                  # Application entry point + orchestrator
│   ├── server.py                # OCPPWebSocketServer (close hook persists Unavailable + last_seen_at)
│   ├── config.py                # Config dataclass from env vars
│   ├── message_handler.py       # OCPP message dispatch
│   ├── connection_manager.py    # Active session tracking
│   ├── timescale_client.py      # TimescaleDB async client (incl. OCPP recovery helpers)
│   ├── ocpp_handler.py          # OCPP 2.0.1 EnhancedOCPPChargePoint
│   ├── ocpp16_adapter.py        # OCPP 1.6 OCPP16Session (wraps FleetChargePoint, reload-on-boot + queue replay)
│   ├── optimization_engine.py   # Heuristic scheduler (legacy)
│   ├── price_feeder.py          # CAISO price ingestion (legacy)
│   ├── analytics_service.py     # Aggregated metrics for REST API
│   ├── security_manager.py      # Auth, TLS, rate limiting
│   └── ...                      # Many additional managers (cache, cert, DER, etc.)
│
├── migrations/                  # SQL schema migrations (run on DB init)
│   ├── 001_initial_schema.sql   # Core schema + seed data
│   ├── 003_vdv463_schema.sql
│   ├── 004_connector_status.sql
│   ├── 005_telemetry_primary_key.sql
│   ├── 012_ocpp_pilot_hardening.sql  # OCPP 1.6 sequences + station_credentials
│   ├── 013_recovery.sql         # charging_command_queue + cross-restart recovery
│   ├── 014_dispatch_queue_notify.sql # queue 'sent' status + pg_notify trigger
│   └── 016_depot_setup_metadata.sql  # depot setup metadata + depot_id indexes
│
├── tests/
│   ├── unit/                    # Unit tests (mock everything)
│   ├── integration/             # Integration tests (require DB)
│   ├── e2e/                     # End-to-end tests
│   ├── performance/             # Performance benchmarks
│   ├── security/                # Security tests
│   ├── load/                    # Load tests
│   └── chaos/                   # Chaos/resilience tests
│
├── scripts/
│   ├── simulation/              # Depot simulation scripts
│   ├── ocpp_simulator.py        # Standalone OCPP charger simulator
│   └── run_migrations.py        # Migration runner
│
├── config/
│   ├── depot_config.yaml        # Depot configuration template
│   └── tariff_config.yaml       # Tariff/rate configuration
│
├── schemas/vdv463/              # JSON schemas for VDV 463 message validation
├── optimization/                # Julia MILP reference implementation
├── monitoring/                  # Prometheus config
├── docs/                        # Architecture and API documentation
├── docker-compose.yml           # Production service definitions
├── docker-compose.test.yml      # Test environment
├── Dockerfile                   # Main API image
├── Dockerfile.ocpp-simulator    # OCPP simulator image
├── pyproject.toml               # Package config, tool settings, dependencies
├── pytest.ini                   # pytest configuration
├── Makefile                     # Development convenience commands
└── .pre-commit-config.yaml      # Pre-commit hook definitions
```

---

## Architecture

### Two Application Layers

The codebase contains **two application layers** that overlap in responsibility:

1. **`src/` (primary, new architecture)** — FastAPI-based REST + OCPP WebSocket server with the new MILP optimizer. This is the canonical implementation going forward.

2. **`src/websocket_handler/` (legacy)** — A standalone OCPP WebSocket handler with its own application orchestrator. Still used for legacy charger communications. Do not delete without verifying no active use.

### Request Flow (new architecture)

```
HTTP/WebSocket client
        │
        ▼
src/api/main.py (FastAPI, middleware: rate-limiting, logging, CORS)
        │
        ├── JWT auth via src/security/auth.py (Supabase JWT)
        │
        ├── REST: /optimize → ControllerManager → DepotController
        │                          → StateAssembler → MILP solver → OCPP dispatch
        │
        ├── REST: /depots/{id}/state|schedule|alerts → DB queries
        │
        ├── REST: /depots/{id}/vehicles/{id}/handoff → interdepot_messages table
        │
        └── WebSocket: /ocpp/{charge_point_id} → OCPPServer → FleetChargePoint
```

### Optimization Control Loop

```
TriggerMonitor (SoC deviation / price spike / return delay / scheduled)
        │
        ▼
StateAssembler.get_current_state()
  ├── vehicle SoCs (telemetry table, max age 15 min)
  ├── prices (prices table, max age 24 hr)
  ├── schedules (departures/returns)
  ├── building_load (required, max age 1 hr)
  └── battery SoC
        │
        ▼
optimizer/milp_model.py → Pyomo ConcreteModel
        │
        ▼
optimizer/solver.py → Gurobi (primary) → HiGHS (fallback)
        │
        ▼
optimizer/allocator.py → per-vehicle charging schedules
        │
        ▼
adapters/ocpp/dispatch.py → SetChargingProfile to each charger
        │
        ▼
DB: optimization_runs, charging_commands tables
```

---

## Hard Constraints (MUST NEVER RELAX)

These come directly from the PRD and are non-negotiable:

| Constraint | Value | PRD Ref |
|---|---|---|
| Vehicle departure SoC | ≥ 99% | Section 8.1 |
| Optimization solve time | < 60 seconds | Section 8.3 |
| Primary OCPP protocol | OCPP 1.6 (2.0.1 future-ready) | Section 9.1 |
| Site grid power constraint (`max_grid_kw`) | NEVER violated | Section 9.4 |
| MVP connector type | CCS only | Section 3.2 |

**Building load is OPTIONAL for initial customer onboarding (e.g. HRX pilot)** — deferred until a meter/BMS integration is delivered. When no live source is configured, the optimizer runs in `degraded` mode and applies a depot-level static `building_load_assumption_kw` as a derate on `max_grid_kw` so the site-power constraint is still respected. The run's `status='degraded'` and the snapshot records the assumption used. Re-introduce as required once meter/BMS lands. (PRD Section 9.4)

---

## Re-optimization Triggers

| Trigger | Threshold | PRD Ref |
|---|---|---|
| Price spike | > 25% OR > $25/MWh (OR logic) | Section 5.1 |
| SoC deviation | > 5% | Section 5.1 |
| Return time deviation | > 15 minutes late | Section 5.1 |
| Inter-depot handoff | On message receipt | Section 5.4 |
| Scheduled | Hourly 24/7 | Section 5.1 |

---

## Database Schema (TimescaleDB / PostgreSQL 16)

### Reference (static) tables
- `organizations` — Customer / workspace tenant (`organization_id` UUID). Rows are **JIT-mirrored** from verified Supabase JWT `app_metadata` (see Tenant mirroring below); canonical org lifecycle lives in Supabase / frontend.
- `user_organizations` — At most one org per user (`user_id` PK → `organization_id`, `role`). JIT-mirrored from JWT `app_metadata` (`organization_id`, `favonius_role`). Name matches the canonical Supabase project schema. **Not** used for API authorization; access control compares JWT claims to `depots.organization_id`.
- `depots` — Physical locations; `max_grid_kw` is the hard site power limit; `organization_id` FK to `organizations`; includes setup metadata fields (`address`, `billing_metadata`, `building_load_source`, `demand_charge_billing_period`, `timezone`, `currency`, `utility_id`)
- `vehicles` — Fleet vehicles; `max_charge_kw` updated from OCPP MeterValues
- `chargers` — EVSE; `ocpp_id` links to OCPP protocol
- `charger_vehicle_access` — Physical accessibility matrix
- `battery_storage` — Stationary batteries

### Time-series hypertables
- `telemetry` — Vehicle SoC, charging_kw, is_plugged (from OCPP MeterValues)
- `prices` — $/kWh by depot and time (CAISO DAM or utility TOU)
- `weather_forecasts` — Temperature, precipitation, solar radiation
- `building_load` — Non-EV site power draw (**required** for grid calc)

### Tenant mirroring (JIT)
- On each authenticated API request, `src/security/tenant_mirror.py` best-effort **UPSERT**s `organizations` and `user_organizations` from the verified JWT payload (`sub`, `app_metadata.organization_id`, `app_metadata.organization_name`, `app_metadata.favonius_role`). If `organization_name` is absent, a deterministic placeholder (`org-<org_uuid_prefix>`) is used for bootstrap rows. **Skips** `favonius_admin` and users without `organization_id`. Failures are logged and do not block the request (depot access still uses JWT vs `depots.organization_id`).
- In-process TTL cache: `TENANT_MIRROR_TTL_S` (default `300`) seconds per `sub` to limit DB writes.
- Workspace **invitations** are managed in Supabase only; there is no `invitations` table in this backend.

### Operational tables
- `schedules` — Vehicle route schedules (departure/return times)
- `optimization_runs` — Solver results, schedule JSON, status, solver_used
- `charging_commands` — OCPP SetChargingProfile records and acknowledgment status (per-run audit, FK to `chargers`)
- `charging_command_queue` — Durable buffer for SetChargingProfile pushes that arrived while a charger was offline; replayed by the legacy WS handler on next BootNotification (migration 013)
- `charging_sessions` — OCPP 1.6 transaction lifecycle. `transaction_id` (BIGINT, from `ocpp_transaction_id` sequence), `last_seen_at` stamped by the WS close hook
- `interdepot_messages` — Cross-depot vehicle handoff messages
- `trigger_log` — Audit trail of re-optimization triggers
- `audit_log` — Application-level admin audit trail (cross-org reads, credential rotations). Distinct from `security_audit_log` (NKSC hypertable). Columns: `id, occurred_at, actor_user_id, actor_role, organization_id, depot_id, action, target_type, target_id, metadata` (JSONB). Common `action` values: `admin.read`, `charger.credentials.rotated`. Written by `src/security/admin_audit.py` from `_record_admin_action` after the endpoint succeeds.
- `connector_status` — OCPP StatusNotification records per connector. Append-only; the latest row per `(station_id, connector_id)` is the current state. The legacy WS handler appends an `Unavailable`/`ConnectionLost` row when the WebSocket drops. Migration 022 attaches the `fn_alerts_on_connector_status` trigger that produces `notification_alerts` rows on Faulted/Unavailable transitions and PERFORMs `pg_notify('notification_alerts_new', …)`.
- `notification_alerts` — Depot/org-scoped alert aggregator (migration 022). Partial unique index `(organization_id, dedup_key) WHERE status != 'resolved'` keeps one active row per fault; resolved rows let new occurrences in. `severity_level` is a generated SMALLINT (1=info, 2=warning, 3=critical). Status: `'active' | 'acknowledged' | 'resolved'`.
- `notification_recipients` — Per-org email subscribers (migration 022). `alert_types` is `TEXT[]` where `'{*}'` matches all types; `min_severity` (with generated `min_severity_level`) gates which alerts the recipient receives.
- `notification_deliveries` — Append-only delivery ledger (migration 022). `UNIQUE (alert_id, recipient_id, notified_count)` is the idempotency anchor. Status: `'sent' | 'delivered' | 'bounced' | 'complained' | 'failed'`. Updated by the `POST /webhooks/resend` handler from Resend events.

### Key columns
- All UUIDs use `gen_random_uuid()` as default
- `solver_used` values: `'gurobi'` | `'highs'`
- `optimization_runs.status` values: `'optimal'` | `'feasible'` | `'degraded'` | `'infeasible'` | `'timeout'`
- `charging_command_queue.status` values: `'pending'` | `'sent'` | `'acked'` | `'failed'` | `'expired'`
- `ocpp_transaction_id` / `ocpp_charging_profile_id` sequences (migration 012) provide restart-safe OCPP 1.6 integer IDs

### Migrations
Migrations in `migrations/` run automatically on `docker-compose up` (mounted to `/docker-entrypoint-initdb.d`). To run manually: `python scripts/run_migrations.py`.

---

## REST API Endpoints

All non-health endpoints require JWT in `Authorization: Bearer <token>` header.

| Method | Path | Description |
|---|---|---|
| `POST` | `/optimize` | Trigger depot MILP optimization |
| `GET` | `/depots/{id}/state` | Current SoCs, battery state, peak demand, price |
| `GET` | `/depots/{id}/schedule` | Latest charging schedule |
| `GET` | `/depots/{id}/alerts` | Charger faults + last optimization + notification_alerts (alerts pipeline) |
| `POST` | `/depots/{id}/alerts/{alert_id}/acknowledge` | Mark a notification alert as acknowledged (alerts pipeline) |
| `POST` | `/depots/{id}/vehicles/{vid}/handoff` | Send inter-depot handoff |
| `POST` | `/depots/{id}/handoff/receive` | Receive inter-depot handoff |
| `GET` | `/health` | Component health (DB, OCPP server, Gurobi license) |
| `GET` | `/metrics` | Prometheus metrics (text format) |
| `GET` | `/admin/controllers` | List active depot controllers |
| `GET` | `/admin/controllers/{id}/health` | Controller health |
| `POST` | `/admin/depots` | Create a tenant-scoped depot in the caller's organization. Safe to call repeatedly; `Idempotency-Key` header required (customer_admin, JWT `app_metadata.organization_id` required) |
| `POST` | `/admin/first-depot-setup` | Backward-compatible alias for `POST /admin/depots`. Same handler; same `Idempotency-Key` requirement |
| `PATCH` | `/admin/depots/{id}` | Update tenant-scoped depot setup (customer_admin + depot access required) |
| `GET` | `/admin/organizations` | List all organizations (favonius_admin only; writes `admin.read` audit row) |
| `GET` | `/admin/organizations/{org_id}/depots` | List depots for an organization (favonius_admin or matching customer_admin; cross-org reads write `admin.read`; mismatched customer_admin → 403, NOT 404) |
| `GET` | `/admin/depots/{id}/chargers/{charger_id}/credentials_status` | `{configured, created_at, last_rotated_at}` only — never plaintext or password_hash (favonius_admin or tenant member; cross-org reads write `admin.read`) |
| `POST` | `/admin/depots/{id}/chargers/{charger_id}/rotate_credentials` | Generate new Basic Auth credential, replace `station_credentials.password_hash`, return plaintext exactly once (favonius_admin or matching customer_admin; writes `charger.credentials.rotated`) |
| `GET` | `/admin/ocpp/{cp_id}/state` | (Legacy WS handler, port 8080) Per-charger debug dump: connection state, vendor/model, last_boot_at, last_heartbeat_at, latest connector_status, open transactions, charging_command_queue rollup. Owner role required. |
| `GET` | `/admin/organizations/{org_id}/notification_recipients` | List alert recipients (favonius_admin or matching customer_admin; cross-org reads write `admin.read`) |
| `POST` | `/admin/organizations/{org_id}/notification_recipients` | Create a recipient (alerts pipeline). 409 on duplicate `(org, email)`. |
| `PATCH` | `/admin/organizations/{org_id}/notification_recipients/{id}` | Patch a recipient |
| `DELETE` | `/admin/organizations/{org_id}/notification_recipients/{id}` | Hard-delete a recipient (cascades deliveries). |
| `POST` | `/webhooks/resend` | Public, signature-verified Resend webhook for delivery status updates (alerts pipeline) |

### WebSocket endpoints
- `ws://host:9000/ocpp/{charge_point_id}` — OCPP 1.6 (dedicated port)
- `ws://host:8000/ocpp/{charge_point_id}` — OCPP via REST port (when `OCPP_USE_SAME_PORT=true`)
- `wss://host/vdv463/{presystem_id}` — VDV 463 transit operations
- `wss://host/bacnet/{device_id}` — BACnet/SC (future)

### Rate limits (per PRD Section 10.4)
- `POST /optimize`: 10 req/min
- `/depots/*/handoff`: 50 msg/hr per depot pair
- All other endpoints: 100 req/min

---

## OCPP Implementation

### Supported operations (OCPP 1.6 primary)
Full coverage of all 28 OCPP 1.6 actions including: BootNotification, Heartbeat, Authorize, StartTransaction, StopTransaction, MeterValues, StatusNotification, ChangeAvailability, ChangeConfiguration, ClearCache, DataTransfer, GetConfiguration, RemoteStartTransaction, RemoteStopTransaction, Reset, SetChargingProfile, ClearChargingProfile, GetCompositeSchedule, UnlockConnector, GetDiagnostics, UpdateFirmware, and more.

### Key OCPP data flows
- `idTag` in Authorize/StartTransaction → looked up against `vehicles.id_tag`; unknown tags get `Invalid`
- `MeterValues` → `telemetry` table (SoC, charging_kw, max_charge_kw updated)
- `StatusNotification` → `connector_status` table (both the new adapter and the legacy `OCPP16Session` write here)
- `StartTransaction` → `transactionId` from `ocpp_transaction_id` sequence; an open `charging_sessions` row is inserted so a handler restart can rehydrate it
- `StopTransaction` → closes the `charging_sessions` row (`end_time`)
- `SetChargingProfile` → after each optimization run the FastAPI service writes one row per scheduled vehicle to `charging_command_queue` (it does NOT push in-process — production runs with `OCPP_SERVER_ENABLED=false`). The legacy WS handler's `ChargingCommandQueueConsumer` (`src/websocket_handler/charging_profile_manager.py::ChargingCommandQueueConsumer`) drains the queue every ~2 s (or on `pg_notify` from migration 014), pushes via the in-memory `OCPP16Session`, and marks rows `sent`/`failed`. Rows whose charger is offline stay `pending`; the BootNotification replay path flushes them on reconnect.

### Cross-restart recovery (legacy handler, migrations 012 + 013 + 014)
- BootNotification: `OCPP16Session._on_boot` reloads open sessions into `FleetChargePoint.transactions` and triggers `replay_queued_commands` so any pending profiles are pushed within ~1s
- WebSocket close: `OCPPWebSocketServer._cleanup_connection` appends an `Unavailable`/`ConnectionLost` row to `connector_status` and stamps `last_seen_at` on every still-open session at the station

### Connector path routing
```
/ocpp/{charge_point_id}    → OCPP 1.6
/vdv463/{presystem_id}     → VDV 463 transit
/bacnet/{device_id}        → BACnet/SC (future)
```

---

## VDV 463 Integration

VDV 463 is the German transit industry standard for BMS (Battery Management System) / ITCS (Intermodal Transport Control System) communication. Favonius acts as the CMS (Charging Management System).

**Validated against schemas in `schemas/vdv463/`:**
- `MessageStructure.json`
- `ProvideChargingRequestsRequest.json`
- `ProvideChargingInformationRequest.json`

**Key behaviors:**
- On validation or semantic errors, return `MessageType 3` (VDV 463 Error message)
- Log structured error details for `InvalidVehicleId` / `InvalidChargingPointId`
- See `.cursor/rules/` for detailed protocol patterns

---

## Surrogate Model (Energy Consumption)

Located in `src/core/surrogate/`. Gaussian Process Regressor (sklearn) following the Stanford CarbonFree approach.

**Input features:** bus_size, route_id, temp_avg_f, temp_max_f, temp_min_f, rain_inches, solar_radiation, is_school_day

**Output:** predicted kWh energy consumption for a trip

**Coverage targets:** ≥ 90% unit test coverage (PRD Section 11.2)

---

## Development Setup

### Install dependencies
```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

### Run locally (Docker for DB)
```bash
# Start TimescaleDB only
docker-compose up -d timescaledb

# Run API server with hot reload
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# Verify
curl http://localhost:8000/health
```

### Full stack with Docker
```bash
# Basic (TimescaleDB + API)
docker-compose up -d

# With OCPP simulator
docker-compose --profile simulation up -d

# With monitoring (Prometheus + Grafana)
docker-compose --profile monitoring up -d

# All services
docker-compose --profile simulation --profile monitoring up -d
```

### Service URLs
- REST API: http://localhost:8000
- API docs: http://localhost:8000/docs
- OCPP WebSocket: ws://localhost:9000/ocpp
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin/admin123)

---

## gstack Skills

gstack is installed globally at `~/.claude/skills/gstack` with individual skills symlinked into `~/.claude/skills/`. Use these slash commands at the appropriate stage of development. Browser-based skills (`/qa`, `/browse`, `/benchmark`, `/canary`, `/setup-browser-cookies`, design-only skills) are excluded — not applicable to this Python backend.

To upgrade: `/gstack-upgrade`. Source: `~/.claude/skills/gstack/`.

### Planning

| Command | When to use |
|---|---|
| `/office-hours` | **Before starting any new feature** — six forcing questions that challenge premises, reframe scope, and surface alternatives before committing to an approach |
| `/plan-ceo-review` | Strategic scope decision (4 modes: expand / selective expand / hold / reduce). Use when debating feature scope with yourself or stakeholders |
| `/plan-eng-review` | **Before coding any non-trivial change** — sequential review: architecture → code quality → tests → performance, one issue at a time with pros/cons. Integrates with the Plan Mode Protocol above |
| `/autoplan` | Hands-off automated CEO + Eng review pipeline. Surfaces only "taste decisions" to the user; auto-decides everything else using completeness, DRY, and pragmatism principles |

> **Integration with Plan Mode Protocol:** `/plan-eng-review` is the preferred execution vehicle for the 4-section review defined in the Plan Mode Protocol above. The BIG CHANGE / SMALL CHANGE scope choice maps directly to gstack's interactive vs. autoplan modes.

### Development & Debugging

| Command | When to use |
|---|---|
| `/review` | Pre-PR staff-engineer code review — two-pass: critical (SQL safety, race conditions, TimescaleDB query patterns, JWT/OCPP trust boundaries, enum completeness) then informational (dead code, test gaps, perf). Auto-fixes what it can, batches ASK findings |
| `/investigate` | **Bug fixing — no fix without root cause first.** 4-phase: symptoms → pattern analysis → hypothesis testing → fix + regression test. Use for solver failures, OCPP connection bugs, DB anomalies, state assembler errors. Scope-locked: won't touch unrelated files |

### Security

| Command | When to use |
|---|---|
| `/cso` | Full 14-phase audit: OWASP Top 10, STRIDE threat modeling, secrets archaeology, supply chain, CI/CD pipeline, LLM/AI security, data classification. Run before releases |
| `/cso --owasp` | OWASP Top 10 only — run after any JWT auth, OCPP handler, or API boundary change |
| `/cso --infra` | Infrastructure only (Phases 0–6) — after Docker, Railway, or DB config changes |
| `/cso --code` | Code-only scan (Phases 0–1, 7, 9–11) — after rate limiter, validator, or auth changes |
| `/cso --comprehensive` | Monthly deep scan with 2/10 confidence gate — surfaces tentative findings |
| `/cso --diff` | Branch-diff only — combinable with any scope flag for PR-scoped audits |

### Shipping & Deployment

| Command | When to use |
|---|---|
| `/ship` | Full PR creation workflow: merge base branch, run test suite, coverage audit (traces all code paths), pre-landing review, version bump, auto-CHANGELOG, PR creation with full evidence body |
| `/land-and-deploy` | Post-PR: waits for CI, merges, detects Railway deploy, polls until live, verifies `/health` endpoint, offers revert commit on failure |
| `/document-release` | After shipping: syncs `docs/API.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md` to reflect what actually shipped. Run after any endpoint, schema, or config change |
| `/setup-deploy` | One-time Railway deploy configuration detection — run when first setting up CI/CD |
| `/gstack-upgrade` | Update gstack to latest version |

### Safety & Guardrails

| Command | When to use |
|---|---|
| `/careful` | Activate at session start for production-adjacent work — intercepts `rm -rf`, `DROP TABLE`, `TRUNCATE`, `git push --force`, `git reset --hard`, `docker system prune` with a warning before execution. Allows `__pycache__` / `.pytest_cache` deletions without warning |
| `/freeze migrations/` | Lock the `migrations/` directory from edits — use when doing work unrelated to schema changes to prevent accidental migration edits |
| `/freeze config/` | Lock `config/` (depot_config.yaml, tariff_config.yaml) during non-config sessions |
| `/unfreeze [path]` | Remove a freeze restriction |
| `/guard` | Combined `/careful` + `/freeze` — activate for high-risk sessions (running migrations, production deploys, dependency upgrades) |

### Retrospective

| Command | When to use |
|---|---|
| `/retro` | Weekly engineering retrospective — commit velocity, per-contributor breakdowns, hotspot files, fix-to-feature ratio, session patterns |
| `/retro compare` | Side-by-side: current week vs prior week |
| `/retro global` | Cross-project retrospective across all AI-assisted coding sessions |

### Excluded Skills (not applicable to this backend)

The following gstack skills are **not symlinked** because they require Chromium or are UI-specific:
`/browse`, `/qa`, `/qa-only`, `/benchmark`, `/canary`, `/setup-browser-cookies`, `/plan-design-review`, `/design-consultation`, `/design-review`.
`/codex` is excluded because its adversarial cross-model review is already embedded inside `/review` (medium/large diffs) and `/ship`.

---

## Testing

### Run tests
```bash
# All tests
pytest

# Unit tests only (no DB required)
pytest tests/unit -v

# With coverage
pytest --cov=src --cov-report=html --cov-report=term

# Specific markers
pytest -m unit
pytest -m integration
pytest -m "not slow"
```

### Via Makefile
```bash
make test              # All tests
make test-unit         # Unit only
make test-integration  # Integration only
make test-coverage     # With HTML coverage report
```

### Coverage requirements
- Overall: ≥ 80% (`fail_under = 80` in pytest.ini)
- Optimizer: ≥ 90% (PRD Section 11.2)
- Surrogate model: ≥ 90% (PRD Section 11.2)

### Test markers
`unit`, `integration`, `e2e`, `load`, `slow`, `docker`, `compliance`, `critical`, `edge_case`, `security`, `acceptance`, `performance`, `database`

### Key test files
- `tests/unit/test_optimizer.py` — MILP model and solver tests
- `tests/unit/test_controller.py` — Control loop tests
- `tests/unit/test_api_main.py` — REST endpoint tests
- `tests/unit/test_ocpp_server_full.py` — Full OCPP coverage tests
- `tests/unit/conftest.py` — Shared fixtures

---

## Code Quality

### Tools
| Tool | Purpose | Config |
|---|---|---|
| `black` | Code formatting | `pyproject.toml`, line-length=100 |
| `isort` | Import sorting | `pyproject.toml`, profile=black |
| `ruff` | Linting + fast formatting | `pyproject.toml` |
| `mypy` | Type checking | `pyproject.toml`, ignore_missing_imports=true |
| `bandit` | Security scanning | `-r src/ -ll` |
| `pydocstyle` | Docstring style | `--convention=google` |

### Run checks
```bash
make lint           # ruff + black --check + isort --check + mypy
make format         # black + isort (auto-fix)
make type-check     # mypy only
pre-commit run --all-files   # All hooks
```

### Standards
- **Type hints**: Required on all functions
- **Docstrings**: Google format
- **Line length**: 100 characters max
- **Python version**: 3.12+
- **Async**: Use `async`/`await` for all I/O operations; use `asyncpg` for DB

---

## Pre-commit Hooks

Configured in `.pre-commit-config.yaml`:
1. trailing-whitespace, end-of-file-fixer, check-yaml, check-json, check-toml
2. check-added-large-files (max 1000KB), check-merge-conflict, mixed-line-ending
3. `black` (formatting)
4. `isort` (import sorting)
5. `ruff` (linting + auto-fix)
6. `mypy` (type checking, excludes tests/ and scripts/)
7. `bandit` (security, excludes tests/)
8. `pydocstyle` (Google convention, excludes tests/ scripts/ migrations/)

---

## Git Workflow

### Commit message format (enforced)
```
type(scope): description
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

**Scope examples:** `optimizer`, `ocpp`, `api`, `db`, `vdv463`, `surrogate`

**Examples:**
```
feat(optimizer): add warm-start support for MILP solver
fix(ocpp): handle duplicate BootNotification without crashing
test(api): add coverage for handoff rate limiting
```

---

## Environment Variables

### Core (required in production)
| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (TimescaleDB) |
| `JWT_SECRET_KEY` | Secret for JWT verification |
| `ENVIRONMENT` | `development` / `staging` / `production` |

### Tenant mirroring (optional)
| Variable | Default | Description |
|---|---|---|
| `TENANT_MIRROR_TTL_S` | `300` | Seconds to cache successful mirror per `sub` (reduces static-DB UPSERTs) |

### OCPP server
| Variable | Default | Description |
|---|---|---|
| `OCPP_SERVER_ENABLED` | `false` | Enable OCPP WebSocket server |
| `OCPP_SERVER_HOST` | `0.0.0.0` | OCPP server bind address |
| `OCPP_SERVER_PORT` | `9000` | OCPP server port |
| `OCPP_USE_SAME_PORT` | `false` | Serve OCPP on same port as REST (for Railway) |
| `WEBSOCKET_PORT` | `9000` | WebSocket server port (legacy handler) |
| `MAX_CONNECTIONS` | `100` | Max concurrent charger sessions |
| `HEARTBEAT_INTERVAL` | `30` | Heartbeat interval in seconds |

### Optimization
| Variable | Default | Description |
|---|---|---|
| `OPTIMIZATION_ENABLED` | `true` | Enable optimization |
| `OPTIMIZATION_TIMEOUT` | `60` | Gurobi solve time limit (seconds) |
| `OPTIMIZATION_MIP_GAP` | `0.01` | MIP optimality gap (1%) |
| `OPTIMIZATION_HORIZON_HOURS` | `4` | Rolling horizon length |
| `OPTIMIZATION_TIMESTEP_MINUTES` | `60` | Decision interval |
| `OPTIMIZATION_SOC_MIN` | `0.2` | Minimum allowed SoC |
| `OPTIMIZATION_SOC_TARGET` | `0.8` | Target SoC before departure |
| `GUROBI_LICENSE_FILE` | — | Path to `gurobi.lic` |
| `GUROBI_LIC_CONTENT` | — | License file contents (for Railway) |

### Price feeder
| Variable | Default | Description |
|---|---|---|
| `PRICE_FEEDER_ENABLED` | `true` | Enable CAISO price ingestion |
| `PRICE_FEEDER_NODES` | `TH_SP15_GEN-APND,...` | CAISO node list |
| `PRICE_FEEDER_FETCH_INTERVAL` | `900` | Fetch interval (seconds) |
| `PRICE_FEEDER_LOOKAHEAD_HOURS` | `24` | Price horizon |
| `PRICE_FEEDER_ENTSOE_ZONES` | — | ENTSO-E EIC zone codes (European depots) |
| `EUROPEAN_ELECTRICITY_API` | — | ENTSO-E API security token |

### Supabase (legacy websocket_handler)
| Variable | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Public anon key |
| `SUPABASE_SERVICE_KEY` | Service role key |
| `SUPABASE_DB_HOST` | Direct DB host |

### Observability
| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |

### Geo-blocking (Article 73-3 compliance)
| Variable | Default | Description |
|---|---|---|
| `GEO_BLOCK_ENABLED` | `true` | Enable geo-blocking middleware |
| `GEO_BLOCK_COUNTRIES` | `RU,CN,BY` | ISO-3166 alpha-2 country codes to block |
| `GEO_BLOCK_ALLOWLIST` | — | Comma-separated IPs/CIDRs that bypass geo-blocking |
| `GEO_BLOCK_FAIL_CLOSED` | `true` | Block requests when GeoIP resolution fails |
| `GEOIP_DB_PATH` | `/app/data/GeoLite2-Country.mmdb` | MaxMind DB path |
| `MAXMIND_LICENSE_KEY` | — | MaxMind license. Set as **both** a build variable (Dockerfile downloads at build, `Dockerfile:54`) **and** a runtime variable (app re-downloads at startup with retries via `_download_geoip_db` if the build-time download was skipped or hit a transient outage). Without it set anywhere, the app fails closed. |

### Alerts pipeline (notifications)
See `docs/plans/alerts-pipeline.md` for the full design.

| Variable | Default | Description |
|---|---|---|
| `EMAIL_DELIVERY_ENABLED` | `true` | Master switch for the AlertDispatcher in the WS handler. False disables both LISTEN and the polling backstop. |
| `RESEND_API_KEY` | — | Resend API bearer token. Without this, the dispatcher runs with `FakeEmailClient` and logs a warning (no real emails). |
| `RESEND_FROM_ADDRESS` | `alerts@favonius.energy` | Default sender address. |
| `RESEND_WEBHOOK_SECRET` | — | HMAC secret for `POST /webhooks/resend` signature verification. Without this every webhook call returns 401. |
| `ALERT_DISPATCHER_POLL_INTERVAL_S` | `30` | Reconciliation cadence; safety net for dropped pg_notify events (decision 4.4). |
| `ALERT_NOTIFY_RESEND_INTERVAL_S` | `3600` | Minimum interval between re-notifications for a still-active alert. |
| `ALERT_DISPATCHER_BATCH_SIZE` | `50` | Maximum alerts processed per dispatcher tick. |
| `WEB_CONCURRENCY` | (unset) | The dispatcher relies on a single-worker assumption (decision 4.1). If this is set above 1, startup logs CRITICAL and double-emails are likely. |

See `.env.example` for full reference with comments.

---

## Dependency Injection & Lifecycle

The FastAPI app uses `@asynccontextmanager` lifespan (`src/api/main.py`):

1. Initialize `asyncpg.Pool` (DB connection pool, min=2, max=10)
2. Optionally start `OCPPServer` (background task or same-port ASGI)
3. Initialize `ControllerManager` → starts per-depot `DepotController` loops

On shutdown: stop controllers → stop OCPP server → close DB pool.

---

## Solver Configuration

**Gurobi (primary, PRD Section 8.2):**
- `TimeLimit=60` seconds
- `MIPGap=0.01` (1%)
- Requires valid license (`gurobi.lic` or `GUROBI_LIC_CONTENT`)

**HiGHS (fallback):**
- Used automatically when Gurobi is unavailable or license fails
- `solver_used` field in `OptimizationResult` records which solver ran

**Julia reference implementation:** `optimization/mip_solver.jl` (not used in production; for algorithm validation only)

---

## Key Data Flows to Remember

### Vehicle → Charger association
`OCPP Authorize(idTag)` → lookup `vehicles.id_tag` → resolve `vehicle_id` for session

### max_charge_kw discovery
`OCPP MeterValues` with `Max.Current.Offered` measurand → update `vehicles.max_charge_kw` and `telemetry.max_charge_kw`

### Grid power balance constraint
```
P_grid[t] = sum(P_vehicle[v,t] for v) + P_batt_charge[t] - P_batt_discharge_effective[t] + P_building[t]
P_grid[t] ≤ max_grid_kw  (hard constraint)
```
Building load (`building_load` table) is **mandatory** in this equation.

### Depot config caching
`_get_depot_config()` in `src/api/main.py` caches `DepotConfig` for 5 minutes (TTL=300s) to reduce DB queries. Invalidate by restarting the API or waiting for TTL expiry.

---

## Acceptance Criteria (PRD Section 11)

Before marking any feature complete, verify:

| ID | Test |
|---|---|
| AT-01 | End-to-End Optimization — schedule generated, applied to chargers |
| AT-02 | Demand Charge Reduction — 30–50% reduction demonstrated |
| AT-03 | Price Spike Re-optimization — triggered within 60s of >25% price jump |
| AT-04 | SoC Deviation Handling — triggered on >5% SoC deviation |
| AT-05 | Return Time Deviation — triggered on >15 min late return |
| AT-06 | Inter-Depot Handoff — vehicle seamlessly handed off between depots |
| AT-07 | Building Load Integration — grid power calc includes building load |
| AT-17 | Alerts Pipeline End-to-End — Faulted → trigger → dispatcher email → Resend webhook → ack via API → recovery → resolve. See `tests/e2e/test_alerts_pipeline_e2e.py` and `docs/plans/alerts-pipeline.md`. |

---

## Deployment

### Railway (cloud)
Two separate services:
1. **API service** — `src/api/main.py` via uvicorn, with `OCPP_SERVER_ENABLED=false` and `OCPP_USE_SAME_PORT=false`
2. **WebSocket Handler service** — `src/websocket_handler/main.py`, with `WEBSOCKET_PORT=$PORT`

Both share the same TimescaleDB instance. See `.env.example` Railway section for required env vars.

### Deployment verification
```bash
./scripts/deploy/verify_deployment.sh
# or
make docker-verify
```

### Health endpoints
- `GET /health` — component status (DB, OCPP, Gurobi license)
- `GET /readiness` — readiness probe (legacy websocket_handler)
- `GET /liveness` — liveness probe (legacy websocket_handler)

---

## Docs Reference

| File | Contents |
|---|---|
| `docs/PRD_v2_7_Building_Integration.md` | **Authoritative spec** |
| `docs/ARCHITECTURE.md` | System architecture |
| `docs/API.md` | API reference |
| `docs/TESTING.md` | Testing guide |
| `docs/DEPLOYMENT.md` | Deployment guide |
| `docs/SIMULATION.md` | Simulation guide |
| `docs/CONTROL_LOOP.md` | Control loop details |
| `docs/DATA_ANALYST_GUIDE.md` | Data access guide |
| `.cursor/rules/optimization.mdc` | MILP patterns |
| `.cursor/rules/ocpp.mdc` | OCPP patterns |
| `.cursor/rules/timescale.mdc` | TimescaleDB patterns |
| `.cursor/rules/favonius-rules.mdc` | General dev rules |
| `~/.claude/skills/gstack/` | gstack skill source (17 skills symlinked to `~/.claude/skills/`) |
| `~/.claude/skills/gstack/ETHOS.md` | Boil the Lake / builder philosophy |
