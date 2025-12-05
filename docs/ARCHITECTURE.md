# System Architecture

## Reference
This document is extracted from the Product Requirements Document. For the authoritative specification, see [PRD.md#5-system-architecture](PRD.md#5-system-architecture).

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL INPUTS                             │
├─────────┬─────────┬─────────┬─────────┬─────────┬─────────────────┤
│ Weather │ Market/ │  Fleet  │ Vehicle │ Inter-  │ Building Load   │
│   API   │ Utility │  Mgmt   │Telemetry│  Depot  │  (optional)     │
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
│  • Charger availability                                             │
│  • Energy consumption forecasts                                     │
│  • Demand charge period info                                        │
│  • Moving peak limit (max grid draw this month)                     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       MILP OPTIMIZER                                │
│                       (Pyomo + HiGHS)                               │
│                                                                     │
│  Objective: min(Energy Cost + Demand Charges)                       │
│  Decision Variables: P_charge[b,t], P_battery[t], y_charge[b,t]     │
│  Hard Constraint: SoC[b, t_depart] ≥ 99%                            │
│                                                                     │
│  Solve time target: < 30 seconds                                    │
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
│                   (Checked every 15 minutes)                        │
├─────────────────────────────────────────────────────────────────────┤
│  Trigger                    │ Threshold                             │
│  ─────────────────────────────────────────────────────────────────  │
│  Vehicle SoC deviation      │ > 5%                                  │
│  Price change               │ > 25% AND > $25/MWh                   │
│  Vehicle return time        │ > 15 minutes                          │
│  Scheduled (default)        │ Hourly 7AM-11PM                       │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **Weather Adapter** | Fetch 7-day forecast | Open-Meteo API, httpx |
| **Price Adapter** | Fetch TOU/CAISO prices | CAISO OASIS, utility APIs |
| **OCPP Server** | Charger communication | ocpp library, WebSocket |
| **Surrogate Model** | Energy consumption prediction | scikit-learn, gpytorch |
| **State Assembler** | Aggregate inputs for optimizer | asyncpg, pandas |
| **MILP Optimizer** | Generate optimal schedules | Pyomo, HiGHS |
| **Trigger Monitor** | Detect re-optimization conditions | asyncio |
| **Control Dispatcher** | Send commands to hardware | OCPP, Modbus |
| **API Server** | External interface | FastAPI, uvicorn |
| **Database** | Persistent storage | TimescaleDB (PostgreSQL) |

## Data Flow

### 1. INGESTION (every 5 minutes)
- Weather API → `weather_forecasts` table
- CAISO API → `prices` table
- OCPP MeterValues → `telemetry` table
- Fleet Mgmt System → `schedules` table

### 2. STATE ASSEMBLY (before each optimization)
- Query: latest telemetry, prices, schedules, depot config
- Compute: vehicle availability windows, energy requirements
- Output: `DepotState` object

### 3. OPTIMIZATION (hourly + triggers)
- Input: `DepotState`, `DepotConfig`
- Execute: MILP solve (< 30s)
- Output: Charging schedule, battery dispatch

### 4. DISPATCH (immediately after optimization)
- OCPP: `SetChargingProfile` to each charger
- Modbus: Battery setpoints
- Database: Store optimization results

### 5. MONITORING (continuous)
- Compare: actual SoC vs. expected SoC
- Compare: current prices vs. baseline prices
- Trigger: re-optimization if thresholds exceeded

## Implementation Structure

The codebase is organized as follows:

- `src/core/optimizer/` - MILP optimization engine
- `src/core/surrogate/` - Energy consumption model
- `src/core/state/` - State assembler
- `src/adapters/ocpp/` - OCPP client/server
- `src/adapters/caiso/` - CAISO price feeds
- `src/adapters/weather/` - Weather API integration
- `src/api/` - FastAPI REST endpoints
- `src/db/` - Database models & migrations

For detailed specifications, see [PRD.md](PRD.md).

