# System Architecture

## Reference
This document is extracted from the Product Requirements Document. For the authoritative specification, see [PRD_v2.md#5-system-architecture](PRD_v2.md#5-system-architecture).

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL INPUTS                             │
├─────────┬─────────┬─────────┬─────────┬─────────┬─────────────────┤
│ Weather │ Market/ │  Fleet  │ Vehicle │ Inter-  │ Building Load   │
│   API   │ Utility │  Mgmt   │Telemetry│  Depot  │    Meter        │
└────┬────┴────┬────┴────┬────┴────┬────┴────┬────┴────────┬────────┘
     │         │         │         │         │             │
     ▼         ▼         ▼         ▼         ▼             ▼
┌─────────────────────────────────────────────────────────────────────┐
│              ENERGY CONSUMPTION SURROGATE MODEL                     │
│              (Gaussian Process / MLP)                               │
│              Input: Weather, Route, Bus Size, Calendar              │
│              Output: E_consumption[kWh]                             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        STATE ASSEMBLER                              │
│                                                                     │
│  • Current SoC (vehicles + battery)                                 │
│  • Price schedule (TOU / CAISO DAM)                                 │
│  • Vehicle availability windows                                     │
│  • Charger availability (aggregated by rated_kw)                  │
│  • Charger-vehicle physical accessibility matrix                    │
│  • Energy consumption forecasts                                     │
│  • Building load forecast                                           │
│  • Demand charge period info                                        │
│  • Moving peak limit (max grid draw this month)                     │
│  • Incoming inter-depot vehicles                                    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       MILP OPTIMIZER                                │
│                       (Pyomo + Gurobi / HiGHS fallback)            │
│                                                                     │
│  Objective: min(Energy Cost + Demand Charges)                       │
│  Decision Variables: P_charge[b,t], P_batt[t], y_charge[b,t]        │
│  Hard Constraint: SoC[b, t_depart] ≥ 99%                            │
│                                                                     │
│  Primary: Gurobi (commercial, high performance)                     │
│  Fallback: HiGHS (open-source, automatic on Gurobi failure)       │
│  Solve time target: < 60 seconds                                    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CONTROL OUTPUT LAYER                             │
├─────────────────┬─────────────────┬─────────────────────────────────┤
│ OCPP Commands   │ Battery Modbus  │ Data Logging (TimescaleDB)      │
│ SetChargingProf │ Charge/Discharge│ Telemetry, Results, Triggers    │
└─────────────────┴─────────────────┴─────────────────────────────────┘

                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   RE-OPTIMIZATION TRIGGERS                          │
├─────────────────────────────────────────────────────────────────────┤
│  Trigger                    │ Detection Method │ Threshold           │
│  ─────────────────────────────────────────────────────────────────  │
│  Vehicle SoC deviation      │ Event-driven     │ > 5%                │
│  Vehicle return time        │ Event-driven     │ > 15 minutes late   │
│  Inter-depot handoff        │ Event-driven     │ On message receipt  │
│  Price change               │ On ingestion     │ > 25% OR > $25/MWh  │
│  Scheduled (default)        │ Periodic         │ Hourly 24/7          │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **Weather Adapter** | Fetch 7-day forecast | Open-Meteo API, httpx |
| **Price Adapter** | Fetch TOU/CAISO prices | CAISO OASIS, utility APIs |
| **Building Load Adapter** | Fetch building power consumption | Modbus meter, API, or forecast |
| **OCPP Server** | Charger communication | ocpp library, WebSocket |
| **Surrogate Model** | Energy consumption prediction | scikit-learn, gpytorch |
| **State Assembler** | Aggregate inputs for optimizer | asyncpg, pandas |
| **MILP Optimizer** | Generate optimal schedules | Pyomo, Gurobi (primary), HiGHS (fallback) |
| **Charger Allocator** | Allocate aggregated power to individual chargers | Post-optimization allocation |
| **Trigger Monitor** | Detect re-optimization conditions | asyncio |
| **Control Dispatcher** | Send commands to hardware | OCPP, Modbus |
| **Handoff Manager** | Send/receive inter-depot messages | HTTP/WebSocket |
| **API Server** | External interface | FastAPI, uvicorn |
| **Database** | Persistent storage | TimescaleDB (PostgreSQL) |

## Data Flow

### 1. INGESTION (every 5 minutes)
- Weather API → `weather_forecasts` table
- CAISO API → `prices` table
- OCPP MeterValues → `telemetry` table (includes vehicle max_charge_kw)
- Fleet Mgmt System → `schedules` table
- Building Load Meter/API → `building_load` table
- Inter-depot Messages → `interdepot_messages` table

### 2. STATE ASSEMBLY (before each optimization)
- Query: latest telemetry, prices, schedules, depot config, building load
- Query: pending inter-depot incoming vehicles (where arrival_time < horizon_end)
- For each incoming vehicle: Add to vehicle_socs with expected_soc, set availability
- Compute: vehicle availability windows, energy requirements
- Compute: charger-vehicle accessibility matrix
- Aggregate: chargers by rated_kw for optimization
- Resolve: demand_charge_rate (prices.demand_kw → depots.demand_charge_rate_kw)
- Output: `DepotState` object

### 3. OPTIMIZATION (hourly + triggers)
- Input: `DepotState`, `DepotConfig`
- Execute: MILP solve (< 60s)
- Output: Charging schedule, battery dispatch

### 4. DISPATCH (immediately after optimization)
- Allocate: Aggregated charger power to individual chargers
- OCPP: `SetChargingProfile` to each charger
- Modbus: Battery setpoints
- Database: Store optimization results

### 5. MONITORING (continuous)
- Compare: actual SoC vs. expected SoC
- Compare: current prices vs. baseline prices
- Trigger: re-optimization if thresholds exceeded
- Cooldown: 5-minute minimum between triggers to prevent rapid re-optimization
- Logging: All trigger events logged with context for analysis

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

