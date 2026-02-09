# System Architecture

## Reference
This document is extracted from the Product Requirements Document. For the authoritative specification, see [PRD_v2.md#5-system-architecture](PRD_v2.md#5-system-architecture).

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
│ Weather │ Market/ │  Fleet  │ Vehicle │ Inter-  │ Building Load   │
│   API   │ Utility │  Mgmt   │Telemetry│  Depot  │    Meter        │
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
│  │  Internal API (Planned - Phase 4)                           │  │
│  │  - Query charge point state                                 │  │
│  │  - Send SetChargingProfile commands                         │  │
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
│  │  • depots                │  │  • telemetry             │       │
│  │  • vehicles              │  │  • prices                │       │
│  │  • chargers              │  │  • weather_forecasts     │       │
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
| **Price Adapter** | Fetch TOU/CAISO prices | CAISO OASIS, utility APIs | Main API |
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
| **Internal API** | Charge point state queries for Main API | HTTP REST | WebSocket Handler (planned Phase 4) |
| **Heuristic Optimizer** | Backup optimization when Main API unavailable | Heuristic algorithms | WebSocket Handler (emergency only) |
| **Supabase** | Static/reference data storage | Supabase (PostgreSQL) | Both services |
| **TimescaleDB** | Time-series data storage | TimescaleDB (PostgreSQL extension) | Both services |

## Data Flow

### 1. INGESTION (every 5 minutes)

**Main API Backend:**
- Weather API → Main API → `weather_forecasts` table (TimescaleDB)
- CAISO API → Main API → `prices` table (TimescaleDB)
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
- Query latest telemetry from TimescaleDB (currently direct query; Phase 4: via WebSocket Handler internal API)
- Query prices, schedules, depot config from Supabase
- Query building load from TimescaleDB
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
- Scheduled: Evaluated hourly 24/7 (configurable: default 7 AM - 11 PM)

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

### 7. BACKUP MODE (WebSocket Handler - Emergency Only)

**Activation Condition:** Main API unavailable for > 1 hour

**Behavior:**
- WebSocket Handler activates heuristic optimizer
- Uses simplified heuristic algorithms (not MILP)
- Ensures basic charging continues during Main API outage
- Logs all actions for post-recovery analysis
- Automatically deactivates when Main API recovers

**Note:** Backup mode is emergency-only and does not meet full optimization requirements

## Implementation Structure

The codebase is organized as follows:

- `src/core/optimizer/` - MILP optimization engine
- `src/core/surrogate/` - Energy consumption model
- `src/core/state/` - State assembler and trigger monitor
- `src/adapters/ocpp/` - OCPP client/server
- `src/adapters/caiso/` - CAISO price feeds
- `src/adapters/weather/` - Weather API integration
- `src/adapters/building_load/` - Building load meter/API
- `src/adapters/handoff/` - Inter-depot handoff manager
- `src/security/` - Security modules (validators, JWT auth, rate limiting, secrets)
- `src/api/` - FastAPI REST endpoints
- `src/db/` - Database models & migrations

For detailed specifications, see [PRD_v2.md](PRD_v2.md).

