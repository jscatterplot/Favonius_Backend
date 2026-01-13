# Component Interaction Diagram

**Date:** 2025-01-XX  
**Purpose:** Visual map of component relationships and data flow

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    EXTERNAL SYSTEMS                              │
├─────────────────────────────────────────────────────────────────┤
│  EV Chargers (OCPP 1.6/2.0.1)  │  CAISO OASIS  │  Open-Meteo   │
│  Building Meters                │  Other Depots │               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              WEBSOCKET HANDLER SERVICE                          │
│              (Telemetry-Only, Separate Service)                │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐                   │
│  │ OCPP Server       │  │ Telemetry        │                   │
│  │ (OCPP 1.6/2.0.1)  │→ │ Ingestion        │                   │
│  └──────────────────┘  └──────────────────┘                   │
│         │                      │                                │
│         └──────────────────────┼──────────────────────────────┘
│                                ▼                                │
│                    ┌──────────────────────┐                    │
│                    │  TimescaleDB Client  │                    │
│                    └──────────────────────┘                    │
│                                │                                │
└────────────────────────────────┼────────────────────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │   TIMESCALEDB            │
                    │   (Time-Series Data)     │
                    │   - telemetry            │
                    │   - prices               │
                    │   - weather_forecasts    │
                    │   - building_load        │
                    │   - optimization_runs    │
                    └─────────────────────────┘
                                 │
                                 │ (reads)
                                 │
┌────────────────────────────────┼────────────────────────────────┐
│              MAIN API BACKEND                                  │
│              (Optimization & Control)                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  FastAPI REST API (src/api/main.py)                      │  │
│  │  - POST /optimize                                        │  │
│  │  - GET /depots/{id}/state                                │  │
│  │  - GET /depots/{id}/schedule                             │  │
│  │  - POST /depots/{id}/handoff/*                           │  │
│  │  - GET /health                                           │  │
│  └──────────────────────────────────────────────────────────┘  │
│           │                                                      │
│           ▼                                                      │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Controller Manager (src/core/controller_manager.py)     │  │
│  │  - Manages multiple DepotController instances            │  │
│  └──────────────────────────────────────────────────────────┘  │
│           │                                                      │
│           ▼                                                      │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Depot Controller (src/core/controller.py)                │  │
│  │  - Main control loop                                      │  │
│  │  - Orchestrates optimization                              │  │
│  │  - Monitors triggers                                      │  │
│  │  - Dispatches OCPP commands                               │  │
│  └──────────────────────────────────────────────────────────┘  │
│           │                                                      │
│           ├──────────────────┬──────────────────┐              │
│           ▼                  ▼                  ▼              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ State        │  │ Trigger      │  │ OCPP         │         │
│  │ Assembler    │  │ Monitor      │  │ Adapter      │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
│           │                  │                  │              │
│           ▼                  │                  │              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Optimization Engine (src/core/optimizer/)               │  │
│  │  - MILP Model Builder                                     │  │
│  │  - Solver (Gurobi/HiGHS)                                  │  │
│  │  - Warm Start                                             │  │
│  │  - Charger Allocator                                      │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Adapters (src/adapters/)                                │  │
│  │  - OCPP Server (command dispatch)                        │  │
│  │  - Weather (Open-Meteo)                                  │  │
│  │  - CAISO (price feed)                                    │  │
│  │  - Handoff Manager (inter-depot)                         │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 │ (reads/writes)
                                 │
                    ┌─────────────────────────┐
                    │   SUPABASE              │
                    │   (Reference Data)       │
                    │   - depots               │
                    │   - vehicles             │
                    │   - chargers             │
                    │   - schedules            │
                    │   - battery_storage      │
                    │   - interdepot_messages  │
                    └─────────────────────────┘
```

## Data Flow

### 1. Telemetry Flow
```
OCPP Charger → WebSocket Handler → TimescaleDB (telemetry table)
                                          ↓
                                    State Assembler (reads)
                                          ↓
                                    Optimization Engine
```

### 2. Optimization Flow
```
Trigger Event → Controller → State Assembler → Optimization Engine
                                                      ↓
                                              Solver (Gurobi/HiGHS)
                                                      ↓
                                              OptimizationResult
                                                      ↓
                                              OCPP Adapter (dispatch)
                                                      ↓
                                              OCPP Charger
```

### 3. Price Data Flow
```
CAISO OASIS → CAISO Adapter → TimescaleDB (prices table)
                                          ↓
                                    State Assembler (reads)
                                          ↓
                                    Optimization Engine
```

### 4. Inter-Depot Handoff Flow
```
Origin Depot → Handoff Manager → HTTP POST → Destination Depot
                                                      ↓
                                              Receive Handoff Endpoint
                                                      ↓
                                              Supabase (interdepot_messages)
                                                      ↓
                                              State Assembler (incoming_vehicles)
                                                      ↓
                                              Optimization Engine
```

## Component Dependencies

### Core Optimization Engine
- **Depends on:** State Assembler, Models, Solver
- **Used by:** Controller, API

### State Assembler
- **Depends on:** Database Pool, Models
- **Reads from:** TimescaleDB, Supabase
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
- **Independent from:** Main API Backend (separate service)

## Key Interactions

1. **Controller → State Assembler:** Requests current depot state for optimization
2. **Controller → Optimizer:** Passes state, receives optimization result
3. **Controller → OCPP Adapter:** Dispatches charging profiles to chargers
4. **Trigger Monitor → Controller:** Triggers re-optimization on events
5. **State Assembler → TimescaleDB:** Reads telemetry, prices, building load
6. **State Assembler → Supabase:** Reads schedules, depot config, vehicles
7. **WebSocket Handler → TimescaleDB:** Writes telemetry (only)

## Separation of Concerns

- **Main API Backend:** Optimization, triggers, command dispatch
- **WebSocket Handler:** OCPP communication, telemetry ingestion (telemetry-only per PRD)
- **TimescaleDB:** Time-series data storage
- **Supabase:** Static/reference data storage
