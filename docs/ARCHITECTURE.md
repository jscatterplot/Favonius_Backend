# System Architecture

This document is the in-repo reference for the platform's service topology, data flow, and database layout. It is the operational source of truth for substrate architecture; product-level direction lives in [PRD_Depot_Agent.md](PRD_Depot_Agent.md).

## Service Architecture Overview

The Favonius platform uses a **two-service architecture** for separation of concerns:

- **Main API Backend**: Primary optimization service that runs MILP optimization, manages triggers, and orchestrates charging schedules
- **WebSocket Handler Service**: Telemetry-only service that handles OCPP communication and stores time-series data

Both services share access to the dual-database architecture (Supabase for reference data, TimescaleDB for time-series data).

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL INPUTS                             │
├─────────┬─────────┬─────────┬─────────┬─────────┬─────────────────┤
│ Weather │ ENTSO-E │  Fleet  │ Vehicle │ Inter-  │ Building Load   │
│   API   │ Day-    │  Mgmt   │Telemetry│  Depot  │    Meter        │
│         │ Ahead   │         │         │         │   (optional)    │
└────┬────┴────┬────┴────┬────┴────┬────┴────┬────┴────────┬────────┘
     │         │         │         │         │             │
     │         │         │         │         │             │
     ▼         ▼         ▼         ▼         ▼             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    MAIN API BACKEND SERVICE                        │
│                    (Optimization & Control)                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  FastAPI REST API                                            │  │
│  │  - POST /optimize                                            │  │
│  │  - GET /depots/{id}/state                                    │  │
│  │  - POST /depots/{id}/handoff/*                               │  │
│  └─────────────────────────────────────────────────────────────┘  │
│           │                                                          │
│           ▼                                                          │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  Controller Manager                                          │  │
│  │  - Manages per-depot controllers                             │  │
│  │  - Orchestrates optimization cycles                          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│           │                                                          │
│           ├──────────────────┬──────────────────┐                  │
│           ▼                  ▼                  ▼                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
│  │ State        │  │ Trigger      │  │ Handoff      │             │
│  │ Assembler    │  │ Monitor      │  │ Manager      │             │
│  └──────────────┘  └──────────────┘  └──────────────┘             │
│           │                  │                  │                  │
│           ▼                  │                  │                  │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  Optimization Engine                                        │  │
│  │  - Surrogate Model (Gaussian Process / MLP)                 │  │
│  │  - MILP Optimizer (Pyomo + Gurobi / HiGHS fallback)         │  │
│  │  - Charger Allocator                                        │  │
│  │                                                              │  │
│  │  Objective: min(Energy Cost + Demand Charges)               │  │
│  │  Hard Constraint: SoC[b, t_depart] ≥ 99%                    │  │
│  │  Solve time target: < 60 seconds                            │  │
│  └─────────────────────────────────────────────────────────────┘  │
│           │                                                          │
│           ▼                                                          │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  Control Dispatcher                                         │  │
│  │  - OCPP SetChargingProfile (via WebSocket Handler)          │  │
│  │  - Battery Modbus commands                                   │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
     │                    │                    │                    │
     │ (queries)           │ (queries)          │ (commands)         │
     │                    │                    │                    │
     ▼                    ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│              WEBSOCKET HANDLER SERVICE                              │
│              (Telemetry & OCPP Communication)                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  OCPP 1.6 WebSocket Server                                   │  │
│  │  - Handles OCPP 2+ messages (backward compatible)            │  │
│  │  - Receives MeterValues, StatusNotification                  │  │
│  │  - Sends SetChargingProfile (from Main API)                  │  │
│  └─────────────────────────────────────────────────────────────┘  │
│           │                                                          │
│           ▼                                                          │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  Telemetry Ingestion                                        │  │
│  │  - Stores MeterValues to TimescaleDB                        │  │
│  │  - Updates vehicle max_charge_kw from OCPP                  │  │
│  │  - Tracks charger_id for all telemetry                      │  │
│  └─────────────────────────────────────────────────────────────┘  │
│  │  Note: Telemetry is stored in the unified `telemetry` table │  │
│  │  (no separate `telemetry_data` table for trial deployments) │  │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  Internal API (POST /internal/ocpp-event)                   │  │
│  │  - OCPP-event ingress to the Main API                       │  │
│  │  - Token-gated via INTERNAL_API_TOKEN (fail-closed)         │  │
│  │  - Health monitoring                                        │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  Backup Heuristic Optimizer (Emergency Only)                │  │
│  │  - Activates if Main API unavailable > 1 hour              │  │
│  │  - Simplified heuristic algorithms                          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
     │                    │
     │ (writes)           │ (writes)
     │                    │
     ▼                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    DUAL DATABASE ARCHITECTURE                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────────────────┐  ┌──────────────────────────┐       │
│  │  SUPABASE                │  │  TIMESCALEDB              │       │
│  │  (PostgreSQL)            │  │  (PostgreSQL Extension)   │       │
│  │                          │  │                          │       │
│  │  Static/Reference Data:  │  │  Time-Series Data:       │       │
│  │  • sites (depots)        │  │  • telemetry             │       │
│  │  • vehicles              │  │  • electricity_prices    │       │
│  │  • charging_stations     │  │  • weather_forecasts     │       │
│  │  • schedules             │  │  • building_load          │       │
│  │  • battery_storage       │  │  • optimization_runs     │       │
│  │  • charger_vehicle_access│  │  • charging_commands      │       │
│  │                          │  │  • interdepot_messages   │       │
│  │  Used by: Both services  │  │  • trigger_log            │       │
│  │                          │  │                          │       │
│  │                          │  │  Used by: Both services  │       │
│  └──────────────────────────┘  └──────────────────────────┘       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                   RE-OPTIMIZATION TRIGGERS                          │
│                   (Main API - Trigger Monitor)                     │
├─────────────────────────────────────────────────────────────────────┤
│  Trigger                    │ Detection Method │ Threshold           │
│  ─────────────────────────────────────────────────────────────────  │
│  Vehicle SoC deviation      │ Event-driven     │ > 5%                │
│  Vehicle return time        │ Event-driven     │ > 15 minutes late   │
│  Inter-depot handoff        │ Event-driven     │ On message receipt  │
│  Price change               │ On ingestion     │ > 25% OR > $25/MWh  │
│  Scheduled (default)        │ Periodic         │ Hourly 24/7          │
│                                                                     │
│  Cooldown: 5 minutes minimum between triggers                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | Technology | Service |
|-----------|---------------|------------|---------|
| **Weather Adapter** | Fetch 7-day forecast | Open-Meteo API, httpx | Main API |
| **Price Adapter** | Fetch day-ahead electricity prices | ENTSO-E Transparency Platform, httpx | Main API |
| **Building Load Adapter** | Fetch building power consumption | Modbus meter, API, or forecast | Main API |
| **Surrogate Model** | Energy consumption prediction | scikit-learn, gpytorch | Main API |
| **State Assembler** | Aggregate inputs for optimizer | asyncpg, pandas | Main API |
| **MILP Optimizer** | Generate optimal schedules | Pyomo, Gurobi (primary), HiGHS (fallback) | Main API |
| **Charger Allocator** | Allocate aggregated power to individual chargers | Post-optimization allocation | Main API |
| **Trigger Monitor** | Detect re-optimization conditions | asyncio | Main API |
| **Control Dispatcher** | Send commands to hardware | OCPP (via WebSocket Handler), Modbus | Main API |
| **Handoff Manager** | Send/receive inter-depot messages | HTTP | Main API |
| **API Server** | External REST interface | FastAPI, uvicorn | Main API |
| **OCPP Server** | Charger communication (OCPP 1.6, handles 2+ messages) | ocpp library, WebSocket | WebSocket Handler |
| **Telemetry Ingestion** | Store OCPP MeterValues to TimescaleDB | asyncpg | WebSocket Handler |
| **Internal API** | OCPP-event ingress to Main API (`POST /internal/ocpp-event`, token-gated via `INTERNAL_API_TOKEN`) | HTTP REST | WebSocket Handler |
| **Heuristic Optimizer** | Backup optimization when Main API unavailable | Heuristic algorithms | WebSocket Handler (emergency only) |
| **Supabase** | Static/reference data storage | Supabase (PostgreSQL) | Both services |
| **TimescaleDB** | Time-series data storage | TimescaleDB (PostgreSQL extension) | Both services |

### WebSocket Handler module classification

- **Runtime‑critical (WebSocket Handler service)**:
  - Entrypoint and orchestration: `src/websocket_handler/main.py`, `server.py`, `connection_manager.py`, `message_handler.py`, `health.py`, `health_checks.py`, `monitoring.py`, `resilience_manager.py`, `task_supervisor.py`.
  - OCPP and security: `ocpp_handler.py`, `ocpp16_adapter.py`, `security_manager.py`, `certificate_manager.py`, `privacy_manager.py`, `pnc_handler.py`.
  - Data plane: `timescale_client.py`, `price_feeder.py`, `optimization_engine.py`, `analytics_service.py`, `connection_monitor.py`, `cache_manager.py`.
- **Legacy‑only / migration candidates**:
  - Supabase‑centric data model and schema tools: `database_schema.py`, `supabase_client.py`, `init_database.py`.
  - Timescale schema tools (replaced in production by `migrations/` + `scripts/run_migrations.py`): `timescale_schema.py`, `init_timescale.py`.
  - Ancillary/admin modules that duplicate newer API responsibilities: `api_server.py`, `display_manager.py`, parts of `analytics_service.py` beyond the basic wrapper.
  - Testing/compliance helpers that are not on the main runtime path: `device_model.py`, `ocpp_schema.py`.

When all chargers have been migrated to the new OCPP server under `src/adapters/ocpp/`
and the MILP optimizer is considered stable, the following `src/websocket_handler`
modules are the primary deprecation candidates:

- Supabase schema + data access: `database_schema.py`, `supabase_client.py`, `init_database.py`.
  These encode the legacy Supabase-first data model and can be removed once all
  static/reference data is managed via the migrations and `src/db/*`.
- Timescale schema bootstrap: `timescale_schema.py`, `init_timescale.py`.
  Production schema creation is owned by `migrations/*.sql` and
  `scripts/run_migrations.py`; these remain useful only as local/dev tooling.
- Duplicate price ingestion and analytics surface: `price_feeder.py`,
  `analytics_service.py`, `api_server.py`, `display_manager.py`.
  Their responsibilities are covered by `src/adapters/caiso/*`, `src/adapters/entsoe/*`,
  and the main FastAPI API; they can be retired once no external callers depend on
  the websocket_handler’s admin/analytics endpoints.
- Testing and compliance helpers used only by websocket_handler tests:
  `device_model.py`, `ocpp_schema.py`. These can be kept in tests-only directories
  or removed if no longer required for OCPP/V2G compliance suites.

## Data Flow

### 1. INGESTION (every 5 minutes)

**Main API Backend:**
- Weather API → Main API → `weather_forecasts` table (TimescaleDB)
- ENTSO-E Transparency Platform (day-ahead) → price feeder → `electricity_prices` table (TimescaleDB), keyed by bidding zone
- Fleet Mgmt System → Main API → `schedules` table (Supabase)
- Building Load Meter/API → Main API → `building_load` table (TimescaleDB)
- Inter-depot Messages → Main API → `interdepot_messages` table (TimescaleDB)

**WebSocket Handler Service:**
- OCPP MeterValues → WebSocket Handler → `telemetry` table (TimescaleDB)
  - Includes: Energy, SoC, Power, max_charge_kw (from vehicle)
  - Stores `charger_id` for all telemetry entries
  - Updates vehicle `max_charge_kw` dynamically from OCPP

### 2. STATE ASSEMBLY (before each optimization - Main API)

**Data Queries:**
- Query latest telemetry from TimescaleDB (direct query)
- Query day-ahead prices (`electricity_prices`) and building load from TimescaleDB
- Query schedules and depot config from Supabase
- Query pending inter-depot incoming vehicles (where `arrival_time < horizon_end`)

**Data Processing:**
- For each incoming vehicle: Add to `vehicle_socs` with `expected_soc`, set availability
- Compute: vehicle availability windows, energy requirements
- Compute: charger-vehicle accessibility matrix
- Aggregate: chargers by `rated_kw` for optimization
- Resolve: `demand_charge_rate` (priority: `prices.demand_kw` → `depots.demand_charge_rate_kw`)

**Output:** `DepotState` object

### 3. OPTIMIZATION (hourly + triggers - Main API)

**Input:** `DepotState`, `DepotConfig`

**Process:**
- Execute: MILP solve using Pyomo/Gurobi (< 60s)
- Fallback: If Gurobi fails (license error, connection issue), automatically use HiGHS solver
- Warm-start: Use previous solution if available (3x speedup)

**Output:** Charging schedule, battery dispatch, `OptimizationResult` with `solver_used` field

### 4. DISPATCH (immediately after optimization - Main API)

**Charger Allocation:**
- Allocate: Aggregated charger power to individual chargers
- Respect: Physical accessibility constraints (`charger_vehicle_access`)
- Prioritize: Vehicles by departure time and SoC deficit

**Command Dispatch:**
- OCPP: `SetChargingProfile` to each charger (currently via Main API's OCPP server; Phase 4: via WebSocket Handler)
- Modbus: Battery setpoints (if battery storage present)
- Database: Store optimization results to `optimization_runs` table (TimescaleDB)

### 5. MONITORING (continuous - Main API)

**Event-Driven Triggers:**
- SoC deviation: Detected within 15 seconds of receiving new telemetry
  - Threshold: > 5% deviation from expected SoC
- Return time deviation: Detected within 15 seconds of schedule update
  - Threshold: > 15 minutes late
- Inter-depot handoff: Detected on message receipt
  - Triggers immediately when handoff message received

**Periodic Triggers:**
- Price change: Evaluated on each price ingestion event (every 5 minutes)
  - Threshold: > 25% OR > $25/MWh (OR logic)
- Scheduled: Evaluated hourly 24/7

**Cooldown:** 5-minute minimum between triggers (configurable via `trigger_cooldown_minutes`)

**Logging:** All trigger events logged to `trigger_log` table with context

### 6. INTER-DEPOT COORDINATION (on vehicle departure - Main API)

**Send Handoff:**
- Origin depot: Creates handoff message with `departure_time`, `expected_soc`, `arrival_time`, `battery_kwh`, `max_charge_kw`
- Sends HTTP POST to destination depot's `/handoff/receive` endpoint
- Stores message in `interdepot_messages` with status='pending'

**Receive Handoff:**
- Destination depot: Validates request, stores message with status='acknowledged'
- Queries original message to get actual `departure_time` (not approximation)
- Incorporates vehicle into next optimization cycle
- Returns acknowledgment with `acknowledged_at` timestamp

### 7. ALERTS PIPELINE (continuous - WebSocket Handler)

The implementation lives in `src/websocket_handler/` (AlertDispatcher), `src/api/main.py` (alert endpoints, Resend webhook), and migration 022; the live code and migration are the source of truth.

```
connector_status INSERT (Faulted/Unavailable)
        │
        ▼
fn_alerts_on_connector_status (PG trigger, migration 022)
  ├── UPSERT into notification_alerts (partial unique on org_id+dedup_key)
  └── pg_notify('notification_alerts_new', JSON payload)
        │
        ▼  (LISTEN connection in WS handler)
AlertDispatcher (single-worker; in-memory _currently_sending set)
  ├── claim_pending_alerts (active AND last_notified_at older than resend window)
  ├── list_for_alert recipients (org_id + alert_type wildcard match + severity threshold)
  ├── render_alert (Jinja2; HTML autoescape, plain-text literal)
  ├── EmailDeliveryClient.send (Resend in prod, Fake in dev/test)
  └── record_delivery (UNIQUE on alert_id+recipient_id+notified_count)
        │
        ▼
notification_deliveries (status='sent')
        │
        ▼  (Resend webhook → POST /webhooks/resend, signature-verified)
notification_deliveries.status updates (delivered | bounced | complained | failed)
```

**Recovery:** When connector_status flips back to a non-fault status, the
trigger updates the matching `notification_alerts` row to `status='resolved'`.
Subsequent occurrences of the same fault create a new alert (the partial
unique index is scoped `WHERE status != 'resolved'`).

**Polling backstop (every 30s):** the dispatcher's main loop runs a
`claim_pending_alerts` tick on a timer in addition to the LISTEN wakeup,
so missed pg_notify events (e.g. WS handler restart, queue overflow) are
recovered within the resend window.

**Single-worker assumption:** the in-memory dedup set is not safe under
multi-worker deployments. Startup logs `CRITICAL` if `WEB_CONCURRENCY > 1`.
See decision 4.1 in the plan.

### 8. BACKUP MODE (WebSocket Handler - Emergency Only)

**Activation Condition:** Main API unavailable for > 1 hour

**Behavior:**
- WebSocket Handler activates heuristic optimizer
- Uses simplified heuristic algorithms (not MILP)
- Ensures basic charging continues during Main API outage
- Logs all actions for post-recovery analysis
- Automatically deactivates when Main API recovers

**Note:** Backup mode is emergency-only and does not meet full optimization requirements

## Component Dependencies

A per-component dependency map (which modules each component reads from, writes to, and is used by). Note that `interdepot_messages` is a **TimescaleDB** table, not Supabase.

### Core Optimization Engine
- **Depends on:** State Assembler, Models, Solver
- **Used by:** Controller, API

### State Assembler
- **Depends on:** Database Pool, Models
- **Reads from:** TimescaleDB (telemetry, `electricity_prices`, building load), Supabase (schedules, depot config, vehicles)
- **Used by:** Controller

### Controller
- **Depends on:** State Assembler, Optimizer, Trigger Monitor, OCPP Adapter
- **Used by:** Controller Manager, API

### OCPP Adapter
- **Depends on:** OCPP Server, Database Pool
- **Writes to:** TimescaleDB (telemetry)
- **Used by:** Controller, WebSocket Handler

### WebSocket Handler
- **Depends on:** OCPP Server, TimescaleDB Client
- **Writes to:** TimescaleDB (telemetry only)
- **Independent from:** Main API Backend (separate service); communicates via `POST /internal/ocpp-event`

### Key Interactions

1. **Controller → State Assembler:** Requests current depot state for optimization
2. **Controller → Optimizer:** Passes state, receives optimization result
3. **Controller → OCPP Adapter:** Dispatches charging profiles to chargers
4. **Trigger Monitor → Controller:** Triggers re-optimization on events
5. **State Assembler → TimescaleDB:** Reads telemetry, `electricity_prices`, building load
6. **State Assembler → Supabase:** Reads schedules, depot config, vehicles
7. **WebSocket Handler → TimescaleDB:** Writes telemetry (only)

## Implementation Structure

The codebase is organized as follows:

- `src/core/optimizer/` - MILP optimization engine
- `src/core/surrogate/` - Energy consumption model
- `src/core/state/` - State assembler and trigger monitor
- `src/adapters/ocpp/` - OCPP client/server
- `src/adapters/entsoe/` - ENTSO-E day-ahead price ingestion (live; lands in `electricity_prices`)
- `src/adapters/caiso/` - CAISO price feeds (deprecated; retained as dead code — Europe-only deployment)
- `src/adapters/weather/` - Weather API integration
- `src/adapters/handoff/` - Inter-depot handoff manager
  - Building load is ingested via the `building_load` table / optional meter or BMS source — there is no dedicated `src/adapters/building_load/` module.
- `src/security/` - Security modules (validators, JWT auth, rate limiting, secrets)
- `src/api/` - FastAPI REST endpoints
- `src/db/` - Database models & migrations

For product direction, see [PRD_Depot_Agent.md](PRD_Depot_Agent.md). Day-to-day operational reference lives in `CLAUDE.md` and the in-repo migration files.

