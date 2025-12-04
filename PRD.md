# Product Requirements Document
## Favonius Energy — EV Fleet Depot Optimization Platform
### Version 1.0 (MVP) | December 2025

---

## Document Purpose

This PRD serves as the **single source of truth** for building the Favonius MVP. It is structured for:
1. **Human engineers** — Clear problem statements, acceptance criteria, and design decisions
2. **AI coding agents (Cursor, Claude Code)** — Precise specifications, code patterns, and validation rules

**How to use this document with Cursor IDE:**
- Reference sections using `@PRD.md#section-name`
- AI agents should check acceptance criteria before marking tasks complete
- Data models are authoritative — do not deviate without updating this document

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Solution Overview](#3-solution-overview)
4. [User Stories & Use Cases](#4-user-stories--use-cases)
5. [System Architecture](#5-system-architecture)
6. [Data Models](#6-data-models)
7. [API Specifications](#7-api-specifications)
8. [Optimization Engine Specifications](#8-optimization-engine-specifications)
9. [Integration Requirements](#9-integration-requirements)
10. [Non-Functional Requirements](#10-non-functional-requirements)
11. [Acceptance Criteria](#11-acceptance-criteria)
12. [Glossary](#12-glossary)

---

## 1. Executive Summary

### 1.1 Product Vision

Favonius Energy delivers an integrated depot energy management platform that coordinates EV charging schedules, stationary batteries, and building loads to **reduce electricity costs by 30-50%** for commercial fleet operators.

### 1.2 MVP Scope

| In Scope (MVP) | Out of Scope (Post-MVP) |
|----------------|------------------------|
| Demand charge minimization | V2G (vehicle-to-grid) |
| EV charging scheduling | Solar generation prediction |
| Stationary battery dispatch | Advanced price forecasting (RL) |
| Market price integration (TOU, CAISO) | Customer dashboard |
| OCPP 1.6/2.0.1 charger control | Mobile app |
| Energy consumption surrogate model | Multi-company deployments |
| Re-optimization triggers | Grid services participation |
| Single-depot operation | Full carbon accounting |
| Inter-depot vehicle handoff messaging | — |

### 1.3 Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Demand charge reduction | ≥ 30% | (Baseline - Optimized) / Baseline |
| Optimization solve time | < 30 seconds | Wall-clock time for 20 vehicles |
| Vehicle departure SoC | 100% of departures ≥ 99% SoC | Telemetry validation |
| System uptime | ≥ 99.5% | Monitoring (excluding maintenance) |
| Energy consumption prediction | R² ≥ 0.85 | 7-day rolling validation |

---

## 2. Problem Statement

### 2.1 The Challenge

Commercial EV fleet operators face **demand charges** that represent **49-90% of their total electricity costs** at charging facilities. A single 15-minute peak in grid demand can add **$2,000-10,000** to monthly bills.

**Current pain points:**
1. **Uncoordinated charging** — Vehicles plug in immediately upon return, creating simultaneous demand spikes
2. **No demand awareness** — Fleet managers lack visibility into how charging affects monthly demand charges
3. **Siloed systems** — Transportation and energy teams operate independently with different priorities
4. **Reactive operations** — No forecasting or optimization of charging schedules

### 2.2 The Opportunity

Fleet vehicles are **idle 85-95% of the time**, providing substantial flexibility for charging schedule optimization. Coordinating charging across a depot can:
- Shift charging to low-cost periods (TOU arbitrage)
- Flatten demand peaks (demand charge reduction)
- Utilize stationary battery storage for peak shaving
- Ensure operational requirements are always met

### 2.3 Constraints

| Constraint | Description | Implication |
|------------|-------------|-------------|
| **Operations first** | Vehicles MUST be fully charged at departure | Hard constraint in optimization |
| **Routes are fixed** | Fleet logistics systems are sacrosanct | Routes/schedules are input, not decision variables |
| **Charger limitations** | Fewer chargers than vehicles (typical: 0.5-0.7 ratio) | Charger assignment is a decision variable |
| **Demand charge periods** | Billing periods vary by utility (15-min, 30-min) | Must track maximum demand per period |

---

## 3. Solution Overview

### 3.1 Core Value Proposition

A software platform that:
1. **Ingests** real-time data (vehicle SoC, schedules, prices, weather)
2. **Predicts** energy consumption using a surrogate model
3. **Optimizes** charging schedules and battery dispatch via MILP
4. **Controls** chargers through OCPP and batteries through Modbus
5. **Monitors** for conditions requiring re-optimization

### 3.2 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **MILP over RL for dispatch** | Guarantees optimal solution with hard constraints; RL reserved for forecasting |
| **24-hour rolling horizon** | Captures diurnal patterns; aligns with day-ahead price markets |
| **15-minute timestep (Δt)** | Matches demand charge intervals; balances fidelity vs. complexity |
| **Gaussian Process surrogate** | Provides uncertainty estimates; proven in Stanford research (R² 0.84-0.94) |
| **Python + Pyomo + HiGHS** | Open-source stack; HiGHS competitive with Gurobi for MILP |
| **TimescaleDB for telemetry** | Optimized for time-series; compression for long-term storage |
| **OCPP 1.6 primary, 2.0.1 ready** | 1.6 is dominant in field; 2.0.1 adds smart charging profiles |

### 3.3 MVP Simplifications

| Simplified Area | MVP Approach | Future Enhancement |
|-----------------|--------------|-------------------|
| Solar prediction | Removed from MVP | Add when self-consumption focus needed |
| Price forecasting | Use market/TOU prices directly | Add RL forecaster for volatile markets |
| V2G | Unidirectional only | Add when market revenue justifies |
| Multi-depot | Single depot optimization | Add coordination layer |

---

## 4. User Stories & Use Cases

### 4.1 Primary Personas

**P1: Fleet Operations Manager**
- Responsible for ensuring vehicles complete routes on time
- Cares about: Vehicle availability, schedule reliability
- Pain point: Doesn't want to think about energy costs

**P2: Facilities/Energy Manager**
- Responsible for managing utility bills and site infrastructure
- Cares about: Cost reduction, demand management
- Pain point: Lacks visibility into fleet charging impact

**P3: Fleet Executive**
- Responsible for fleet TCO and sustainability reporting
- Cares about: Cost savings, ROI, carbon metrics
- Pain point: Can't quantify value of smart charging

### 4.2 User Stories

#### US-01: Automated Charging Schedule
```
AS A fleet operations manager
I WANT charging schedules automatically generated
SO THAT I don't have to manually coordinate charging
AND vehicles are always ready for their routes
```

**Acceptance Criteria:**
- [ ] System generates 24-hour charging schedule without manual input
- [ ] All vehicles reach ≥99% SoC by their scheduled departure time
- [ ] Schedule respects charger capacity constraints
- [ ] Schedule is updated hourly and on trigger events

#### US-02: Demand Charge Reduction
```
AS A facilities manager
I WANT the system to minimize demand charges
SO THAT I can reduce monthly electricity costs
```

**Acceptance Criteria:**
- [ ] System tracks maximum demand per billing period
- [ ] Optimization objective includes demand charge component
- [ ] Month-to-date peak demand is visible via API
- [ ] Battery storage dispatched to shave peaks

#### US-03: Re-optimization on Price Spikes
```
AS A facilities manager
I WANT the system to re-optimize when prices change significantly
SO THAT I can take advantage of price arbitrage opportunities
```

**Acceptance Criteria:**
- [ ] Price change threshold: >25% AND >$25/MWh
- [ ] Re-optimization completes within 60 seconds of trigger
- [ ] New schedule dispatched to chargers automatically
- [ ] Trigger reason logged for analysis

#### US-04: Vehicle SoC Deviation Handling
```
AS A fleet operations manager
I WANT the system to adjust schedules if vehicle SoC deviates from plan
SO THAT departure requirements are still met
```

**Acceptance Criteria:**
- [ ] SoC deviation threshold: >5%
- [ ] Re-optimization triggered within 1 minute of detection
- [ ] New schedule prioritizes affected vehicles
- [ ] No impact on other vehicles' departure requirements

#### US-05: Inter-depot Vehicle Handoff
```
AS A fleet operations manager
I WANT the destination depot notified when a vehicle is en route
SO THAT charging can be planned before arrival
```

**Acceptance Criteria:**
- [ ] Message sent when vehicle departs origin depot
- [ ] Message includes: vehicle ID, expected SoC, arrival time
- [ ] Destination depot incorporates vehicle into next optimization
- [ ] Handoff logged for audit trail

---

## 5. System Architecture

### 5.1 High-Level Architecture

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

### 5.2 Component Responsibilities

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

### 5.3 Data Flow

```
1. INGESTION (every 5 minutes)
   Weather API → weather_forecasts table
   CAISO API → prices table
   OCPP MeterValues → telemetry table
   Fleet Mgmt System → schedules table

2. STATE ASSEMBLY (before each optimization)
   Query: latest telemetry, prices, schedules, depot config
   Compute: vehicle availability windows, energy requirements
   Output: DepotState object

3. OPTIMIZATION (hourly + triggers)
   Input: DepotState, DepotConfig
   Execute: MILP solve (< 30s)
   Output: Charging schedule, battery dispatch

4. DISPATCH (immediately after optimization)
   OCPP: SetChargingProfile to each charger
   Modbus: Battery setpoints
   Database: Store optimization results

5. MONITORING (continuous)
   Compare: actual SoC vs. expected SoC
   Compare: current prices vs. baseline prices
   Trigger: re-optimization if thresholds exceeded
```

---

## 6. Data Models

### 6.1 Database Schema

```sql
-- PostgreSQL + TimescaleDB

-- ============ REFERENCE DATA ============

CREATE TABLE depots (
    depot_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    timezone        VARCHAR(50) DEFAULT 'America/Los_Angeles',
    utility_id      VARCHAR(100),
    max_grid_kw     DOUBLE PRECISION NOT NULL,
    demand_charge_rate_kw  DOUBLE PRECISION DEFAULT 20.0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE vehicles (
    vehicle_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    external_id     VARCHAR(100) UNIQUE,  -- customer's vehicle ID
    vehicle_type    VARCHAR(50) NOT NULL,  -- 'bus_large', 'bus_small', 'van'
    battery_kwh     DOUBLE PRECISION NOT NULL,
    max_charge_kw   DOUBLE PRECISION NOT NULL,
    ocpp_id         VARCHAR(100),  -- charge point identifier
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE chargers (
    charger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    ocpp_id         VARCHAR(100) UNIQUE NOT NULL,
    rated_kw        DOUBLE PRECISION NOT NULL,
    efficiency      DOUBLE PRECISION DEFAULT 0.95,
    connector_type  VARCHAR(50),  -- 'CCS', 'CHAdeMO', 'Type2'
    status          VARCHAR(20) DEFAULT 'Available',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE battery_storage (
    battery_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    capacity_kwh    DOUBLE PRECISION NOT NULL,
    max_power_kw    DOUBLE PRECISION NOT NULL,
    efficiency      DOUBLE PRECISION DEFAULT 0.92,
    soc_min         DOUBLE PRECISION DEFAULT 0.2,
    soc_max         DOUBLE PRECISION DEFAULT 0.8,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============ TIME-SERIES DATA ============

CREATE TABLE telemetry (
    time            TIMESTAMPTZ NOT NULL,
    vehicle_id      UUID NOT NULL,
    soc             DOUBLE PRECISION,  -- 0.0 to 1.0
    location_lat    DOUBLE PRECISION,
    location_lon    DOUBLE PRECISION,
    is_plugged      BOOLEAN,
    charging_kw     DOUBLE PRECISION,
    odometer_km     DOUBLE PRECISION
);
SELECT create_hypertable('telemetry', 'time');
CREATE INDEX idx_telemetry_vehicle ON telemetry (vehicle_id, time DESC);

CREATE TABLE prices (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    energy_kwh      DOUBLE PRECISION NOT NULL,  -- $/kWh
    demand_kw       DOUBLE PRECISION,           -- $/kW (if different by period)
    source          VARCHAR(50),                -- 'caiso_dam', 'utility_tou'
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('prices', 'time');

CREATE TABLE weather_forecasts (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    temp_f          DOUBLE PRECISION,
    temp_max_f      DOUBLE PRECISION,
    temp_min_f      DOUBLE PRECISION,
    precip_in       DOUBLE PRECISION,
    solar_rad       DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('weather_forecasts', 'time');

-- ============ OPERATIONAL DATA ============

CREATE TABLE schedules (
    schedule_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    route_id        VARCHAR(100),
    departure_time  TIMESTAMPTZ NOT NULL,
    return_time     TIMESTAMPTZ NOT NULL,
    energy_kwh      DOUBLE PRECISION,  -- estimated consumption
    required_soc    DOUBLE PRECISION DEFAULT 1.0,
    dest_depot_id   UUID REFERENCES depots(depot_id),  -- if different
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_schedules_vehicle_depart ON schedules (vehicle_id, departure_time);

CREATE TABLE optimization_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    run_time        TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  VARCHAR(50) NOT NULL,  -- 'scheduled', 'price_spike', 'soc_deviation'
    horizon_start   TIMESTAMPTZ NOT NULL,
    horizon_end     TIMESTAMPTZ NOT NULL,
    solve_time_s    DOUBLE PRECISION,
    objective_value DOUBLE PRECISION,
    peak_demand_kw  DOUBLE PRECISION,
    status          VARCHAR(20) DEFAULT 'completed',
    schedule_json   JSONB NOT NULL
);

CREATE TABLE charging_commands (
    command_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID REFERENCES optimization_runs(run_id),
    charger_id      UUID NOT NULL REFERENCES chargers(charger_id),
    vehicle_id      UUID REFERENCES vehicles(vehicle_id),
    issued_at       TIMESTAMPTZ DEFAULT NOW(),
    profile_json    JSONB NOT NULL,
    status          VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'accepted', 'rejected'
    response_at     TIMESTAMPTZ
);

CREATE TABLE interdepot_messages (
    message_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_depot_id UUID NOT NULL REFERENCES depots(depot_id),
    dest_depot_id   UUID NOT NULL REFERENCES depots(depot_id),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    departure_time  TIMESTAMPTZ NOT NULL,
    expected_soc    DOUBLE PRECISION NOT NULL,
    arrival_time    TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ
);
```

### 6.2 Python Data Classes

```python
# src/core/models.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID


@dataclass
class Depot:
    depot_id: UUID
    name: str
    latitude: float
    longitude: float
    timezone: str
    max_grid_kw: float
    demand_charge_rate_kw: float


@dataclass
class Vehicle:
    vehicle_id: UUID
    depot_id: UUID
    external_id: str
    vehicle_type: str  # 'bus_large', 'bus_small', 'van'
    battery_kwh: float
    max_charge_kw: float
    ocpp_id: Optional[str] = None


@dataclass
class Charger:
    charger_id: UUID
    depot_id: UUID
    ocpp_id: str
    rated_kw: float
    efficiency: float = 0.95
    status: str = 'Available'


@dataclass
class BatteryStorage:
    battery_id: UUID
    depot_id: UUID
    capacity_kwh: float
    max_power_kw: float
    efficiency: float = 0.92
    soc_min: float = 0.2
    soc_max: float = 0.8


@dataclass
class Schedule:
    schedule_id: UUID
    vehicle_id: UUID
    route_id: str
    departure_time: datetime
    return_time: datetime
    energy_kwh: float
    required_soc: float = 1.0
    dest_depot_id: Optional[UUID] = None


@dataclass
class DepotConfig:
    """Static configuration for optimization."""
    vehicle_capacities: dict[str, float]  # vehicle_id -> kWh
    charger_power: float
    charger_efficiency: float
    n_chargers: int
    battery_capacity: float
    battery_power: float
    max_site_power: float
    delta_t: float = 0.25  # hours (15 min)
    n_timesteps: int = 96  # 24 hours


@dataclass
class DepotState:
    """Dynamic state for optimization."""
    vehicle_socs: dict[str, float]           # vehicle_id -> SoC [0,1]
    battery_soc: float
    prices: list[float]                      # $/kWh per timestep
    demand_charge_rate: float                # $/kW
    current_month_peak: float                # kW
    vehicle_availability: dict[str, list[bool]]
    energy_requirements: dict[str, float]    # vehicle_id -> kWh needed
    departure_times: dict[str, int]          # vehicle_id -> timestep index


@dataclass
class OptimizationResult:
    """Output from optimization."""
    run_id: UUID
    schedule: dict[str, dict]  # vehicle_id -> {charging_power: [], soc: []}
    battery_dispatch: list[float]
    grid_power: list[float]
    peak_demand: float
    objective_value: float
    solve_time: float
    status: str
```

---

## 7. API Specifications

### 7.1 REST API Endpoints

#### POST /optimize
Trigger optimization for a depot.

**Request:**
```json
{
    "depot_id": "uuid",
    "horizon_hours": 24,
    "force": false
}
```

**Response:**
```json
{
    "run_id": "uuid",
    "depot_id": "uuid",
    "status": "completed",
    "objective_value": 1234.56,
    "solve_time_seconds": 12.3,
    "peak_demand_kw": 450.0,
    "schedule": {
        "bus_1": {
            "charging_power": [0, 0, 80, 80, ...],
            "soc": [0.3, 0.3, 0.35, 0.40, ...]
        }
    }
}
```

**Error Codes:**
- 400: Invalid request (missing depot_id, etc.)
- 404: Depot not found
- 500: Optimization failed (infeasible, timeout)

---

#### GET /depots/{depot_id}/state
Get current depot state.

**Response:**
```json
{
    "depot_id": "uuid",
    "timestamp": "2025-12-04T10:00:00Z",
    "vehicle_socs": {
        "bus_1": 0.45,
        "bus_2": 0.82
    },
    "battery_soc": 0.55,
    "current_month_peak_kw": 380.0,
    "current_price_kwh": 0.15
}
```

---

#### GET /depots/{depot_id}/schedule
Get current charging schedule.

**Response:**
```json
{
    "depot_id": "uuid",
    "run_id": "uuid",
    "generated_at": "2025-12-04T09:00:00Z",
    "horizon_start": "2025-12-04T09:00:00Z",
    "horizon_end": "2025-12-05T09:00:00Z",
    "schedule": { ... }
}
```

---

#### POST /depots/{depot_id}/vehicles/{vehicle_id}/handoff
Send inter-depot handoff message.

**Request:**
```json
{
    "dest_depot_id": "uuid",
    "expected_soc": 0.35,
    "arrival_time": "2025-12-04T14:30:00Z"
}
```

**Response:**
```json
{
    "message_id": "uuid",
    "status": "sent"
}
```

---

#### GET /health
Health check endpoint.

**Response:**
```json
{
    "status": "healthy",
    "timestamp": "2025-12-04T10:00:00Z",
    "components": {
        "database": "healthy",
        "ocpp_server": "healthy"
    }
}
```

---

### 7.2 WebSocket API (OCPP)

The platform implements an OCPP 1.6 Central System at `ws://<host>:9000/{charger_id}`.

**Supported Messages:**
| Direction | Message | Purpose |
|-----------|---------|---------|
| CP → CS | BootNotification | Charger registration |
| CP → CS | StatusNotification | Charger status updates |
| CP → CS | MeterValues | Energy and SoC readings |
| CP → CS | StartTransaction | Charging session start |
| CP → CS | StopTransaction | Charging session end |
| CS → CP | SetChargingProfile | Dispatch charging schedule |
| CS → CP | RemoteStartTransaction | Initiate charging |
| CS → CP | RemoteStopTransaction | Stop charging |

---

## 8. Optimization Engine Specifications

### 8.1 Mathematical Formulation

**Objective Function:**
```
minimize:
    Σ_t (price[t] × P_grid[t] × Δt)           # Energy cost
  + demand_rate × P_max_grid                   # Demand charge
```

**Decision Variables:**
| Variable | Domain | Description |
|----------|--------|-------------|
| P_charge[b,t] | ℝ≥0 | Charging power for vehicle b at time t (kW) |
| SoC[b,t] | [0.1, 1.0] | State of charge for vehicle b at time t |
| y_charge[b,t] | {0,1} | Binary: is vehicle b charging at time t |
| P_batt[t] | [-P_max, P_max] | Battery power (+discharge, -charge) |
| SoC_batt[t] | [0.2, 0.8] | Battery state of charge |
| P_grid[t] | ℝ≥0 | Grid power draw at time t |
| P_max_grid | ℝ≥0 | Maximum grid power (demand) |

**Constraints:**

1. **SoC Dynamics:**
   ```
   SoC[b,t] = SoC[b,t-1] + (η × P_charge[b,t-1] × Δt) / E_batt[b]
   ```

2. **Vehicle Availability:**
   ```
   P_charge[b,t] = 0   if available[b,t] = 0
   ```

3. **Departure SoC (HARD):**
   ```
   SoC[b, t_depart[b]] ≥ 0.99
   ```

4. **Charger Linking:**
   ```
   P_charge[b,t] ≤ P_charger × y_charge[b,t]
   ```

5. **Charger Capacity:**
   ```
   Σ_b y_charge[b,t] ≤ n_chargers
   ```

6. **Grid Power Balance:**
   ```
   P_grid[t] = Σ_b P_charge[b,t] + P_building[t] - P_batt[t]
   ```

7. **Demand Tracking:**
   ```
   P_max_grid ≥ P_grid[t]   ∀t
   P_max_grid ≥ current_month_peak   (moving limit)
   ```

8. **Battery Dynamics:**
   ```
   SoC_batt[t] = SoC_batt[t-1] + (P_batt[t-1] × Δt) / E_batt_storage
   0.2 ≤ SoC_batt[t] ≤ 0.8
   ```

### 8.2 Solver Configuration

```python
# Pyomo + HiGHS configuration
solver = pyo.SolverFactory('appsi_highs')
solver.options['time_limit'] = 30.0      # seconds
solver.options['mip_rel_gap'] = 0.01     # 1% optimality gap
solver.options['threads'] = 4            # parallel threads
solver.options['presolve'] = 'on'        # preprocessing
```

### 8.3 Performance Targets

| Metric | Target | Test Configuration |
|--------|--------|-------------------|
| Solve time | < 30 seconds | 20 vehicles, 96 timesteps |
| Memory | < 2 GB | Same configuration |
| Optimality gap | < 1% | Required for deployment |
| Warm-start speedup | > 3x | Use previous solution |

### 8.4 Surrogate Model Specification

**Model Type:** Gaussian Process Regression (fallback: 2-layer MLP)

**Features:**
| Feature | Type | Source |
|---------|------|--------|
| bus_size | Categorical | Fleet config |
| route_id | Categorical | Schedule |
| temp_avg_f | Continuous | Weather API |
| temp_max_f | Continuous | Weather API |
| temp_min_f | Continuous | Weather API |
| rain_inches | Continuous | Weather API |
| solar_radiation | Continuous | Weather API |
| heating_degree_days | Derived | temp_avg_f |
| cooling_degree_days | Derived | temp_avg_f |
| is_school_day | Binary | Calendar |

**Training:**
- Window: Rolling 30 days
- Validation: Next 7 days
- Retraining: Weekly

**Targets:**
- R² ≥ 0.85 on validation set
- Uncertainty estimates for robust optimization

---

## 9. Integration Requirements

### 9.1 OCPP Integration

**Supported Versions:** OCPP 1.6-J (primary), OCPP 2.0.1 (future)

**Connection Flow:**
```
1. Charger connects: ws://platform:9000/{ocpp_id}
2. Platform sends: BootNotificationResponse (Accepted, interval=300)
3. Charger sends: StatusNotification (every 5 min)
4. Charger sends: MeterValues (every 15 sec when charging)
5. Platform sends: SetChargingProfile (after optimization)
```

**SetChargingProfile Structure:**
```json
{
    "connectorId": 1,
    "csChargingProfiles": {
        "chargingProfileId": 1,
        "stackLevel": 0,
        "chargingProfilePurpose": "TxProfile",
        "chargingProfileKind": "Absolute",
        "chargingSchedule": {
            "chargingRateUnit": "W",
            "chargingSchedulePeriod": [
                {"startPeriod": 0, "limit": 80000, "numberPhases": 3},
                {"startPeriod": 900, "limit": 60000, "numberPhases": 3},
                {"startPeriod": 1800, "limit": 80000, "numberPhases": 3}
            ]
        }
    }
}
```

### 9.2 Weather API (Open-Meteo)

**Endpoint:** `https://api.open-meteo.com/v1/forecast`

**Parameters:**
- latitude, longitude: Depot location
- daily: temperature_2m_max, temperature_2m_min, precipitation_sum, shortwave_radiation_sum
- forecast_days: 7

**Rate Limits:** 10,000 requests/day (free tier)

### 9.3 Price Data

**CAISO OASIS (California):**
- Endpoint: `http://oasis.caiso.com/oasisapi/SingleZip`
- Data: Day-Ahead LMP by node
- Update: Daily at 10:00 AM PT

**Utility TOU (Fallback):**
- PG&E E-19: Peak (4-9 PM), Partial-Peak (9 AM-4 PM, 9-12 PM), Off-Peak
- Store in depot configuration

### 9.4 Fleet Management System

**Expected Input Format:**
```json
{
    "schedules": [
        {
            "vehicle_id": "bus_1",
            "route_id": "route_101",
            "departure_time": "2025-12-04T06:00:00Z",
            "return_time": "2025-12-04T14:00:00Z",
            "estimated_miles": 120
        }
    ]
}
```

**Integration Options:**
1. Direct API integration (preferred)
2. CSV file upload
3. Manual entry via admin interface

---

## 10. Non-Functional Requirements

### 10.1 Performance

| Requirement | Target | Measurement |
|-------------|--------|-------------|
| Optimization latency | < 30 seconds | 95th percentile |
| API response time | < 500 ms | 99th percentile |
| Database query time | < 100 ms | Average |
| OCPP message latency | < 2 seconds | End-to-end |

### 10.2 Reliability

| Requirement | Target |
|-------------|--------|
| System availability | ≥ 99.5% |
| Data durability | 99.99% (TimescaleDB replication) |
| Optimization success rate | ≥ 99% (feasible solutions) |
| OCPP connection uptime | ≥ 99% |

### 10.3 Security

| Requirement | Implementation |
|-------------|---------------|
| API authentication | JWT tokens |
| OCPP authentication | Basic auth + TLS |
| Database access | Role-based, encrypted connections |
| Secrets management | Environment variables, Vault (future) |
| Audit logging | All API calls, optimization runs |

### 10.4 Scalability

| Dimension | MVP Target | Future |
|-----------|-----------|--------|
| Vehicles per depot | 50 | 200 |
| Depots | 5 | 100 |
| Telemetry ingestion | 100 msg/sec | 10,000 msg/sec |
| Optimization parallelism | Serial | Per-depot parallel |

---

## 11. Acceptance Criteria

### 11.1 MVP Acceptance Tests

#### AT-01: End-to-End Optimization
```gherkin
GIVEN a depot with 10 vehicles and 5 chargers
AND 3 vehicles need to depart at 6:00 AM with 100% SoC
AND current time is 10:00 PM previous day
WHEN optimization is triggered
THEN all 3 departing vehicles reach ≥99% SoC by 5:45 AM
AND solve time is < 30 seconds
AND charging schedule is dispatched to chargers
```

#### AT-02: Demand Charge Reduction
```gherkin
GIVEN a depot with 200 kW current month peak
AND unmanaged charging would cause 300 kW peak
WHEN optimization runs for a 24-hour horizon
THEN optimized peak is ≤ 220 kW
AND demand charge savings ≥ $1,600/month (at $20/kW)
```

#### AT-03: Price Spike Re-optimization
```gherkin
GIVEN an active charging schedule
AND prices increase from $0.10/kWh to $0.15/kWh (50% increase, +$50/MWh)
WHEN the price trigger fires
THEN re-optimization starts within 60 seconds
AND new schedule shifts charging away from high-price period
```

#### AT-04: SoC Deviation Handling
```gherkin
GIVEN bus_1 expected SoC = 0.60 at 2:00 PM
AND actual SoC = 0.52 (8% deviation)
WHEN trigger monitor detects deviation
THEN re-optimization is triggered
AND new schedule prioritizes bus_1 charging
AND bus_1 still meets departure requirement
```

#### AT-05: Inter-Depot Handoff
```gherkin
GIVEN bus_1 departing depot_A for depot_B
AND expected arrival SoC = 0.35
WHEN bus_1 departs depot_A
THEN depot_B receives handoff message within 30 seconds
AND depot_B's next optimization includes bus_1
```

### 11.2 Unit Test Requirements

| Module | Coverage Target | Critical Paths |
|--------|-----------------|---------------|
| Optimizer | ≥ 90% | Constraint satisfaction, objective calculation |
| Surrogate Model | ≥ 85% | Feature engineering, prediction |
| State Assembler | ≥ 80% | Data aggregation, availability computation |
| OCPP Adapter | ≥ 85% | Message handling, profile dispatch |
| Trigger Monitor | ≥ 90% | All trigger conditions |

### 11.3 Integration Test Requirements

| Test Case | Expected Behavior |
|-----------|-------------------|
| Full optimization cycle | State → Optimize → Dispatch → Verify |
| OCPP charger simulation | Connect, MeterValues, SetChargingProfile |
| Database failover | Graceful degradation, no data loss |
| Price feed failure | Fall back to cached prices |
| Weather API timeout | Use last known forecast |

---

## 12. Glossary

| Term | Definition |
|------|------------|
| **CAISO** | California Independent System Operator — manages California grid |
| **DAM** | Day-Ahead Market — electricity market clearing day before delivery |
| **Demand Charge** | Monthly fee based on peak power draw ($/kW) |
| **Δt** | Optimization timestep (15 minutes = 0.25 hours) |
| **GP** | Gaussian Process — probabilistic ML model for surrogate |
| **HiGHS** | Open-source MILP solver |
| **Horizon** | Planning window for optimization (typically 24 hours) |
| **LMP** | Locational Marginal Price — electricity price at a grid node |
| **MILP** | Mixed-Integer Linear Programming |
| **MPC** | Model Predictive Control — rolling horizon optimization |
| **OCPP** | Open Charge Point Protocol — charger communication standard |
| **SoC** | State of Charge — battery level (0-100%) |
| **TOU** | Time-of-Use — electricity rate structure varying by time |
| **V2G** | Vehicle-to-Grid — bidirectional charging (out of MVP scope) |
| **Warm-start** | Initialize solver with previous solution |

---

## Appendix A: Cursor IDE Integration

### A.1 Project Rules (.cursorrules)

```
You are a senior Python developer specializing in energy systems optimization.

Project: Favonius Energy - EV Fleet Depot Optimization Platform
Tech Stack: Python 3.12, Pyomo, HiGHS, FastAPI, TimescaleDB, OCPP

Key Patterns:
- Use dataclasses for data models (see src/core/models.py)
- Use asyncpg for database operations
- Use Pyomo for optimization modeling
- Follow the Stanford CarbonFree paper approach for surrogate models

Critical Constraints:
- Vehicle departure SoC ≥ 99% is a HARD constraint (never relax)
- Optimization solve time MUST be < 30 seconds
- OCPP 1.6 is primary protocol version

When implementing optimization:
- Reference PRD.md#8-optimization-engine-specifications for formulation
- Use warm-starting from previous solutions
- Log solve time and objective value

When implementing API endpoints:
- Reference PRD.md#7-api-specifications for contracts
- Return consistent error formats
- Include request/response logging

Code Style:
- Type hints required on all functions
- Docstrings in Google format
- Max line length: 100
```

### A.2 Context Files

Place these in your Cursor project for automatic context:

1. `docs/PRD.md` — This document
2. `docs/ARCHITECTURE.md` — System architecture diagram
3. `docs/API.md` — OpenAPI specification
4. `src/core/models.py` — Data models
5. `.cursorrules` — Project rules

### A.3 Recommended Prompts

**For implementing a new feature:**
```
Implement US-02 (Demand Charge Reduction) following the specifications in @PRD.md#4-user-stories--use-cases.
Use the optimization formulation from @PRD.md#8-optimization-engine-specifications.
```

**For debugging optimization:**
```
The optimizer is returning infeasible. Check constraints against @PRD.md#8-1-mathematical-formulation.
Verify departure SoC constraint is correctly implemented.
```

**For adding a new API endpoint:**
```
Add the endpoint specified in @PRD.md#7-1-rest-api-endpoints.
Follow the existing patterns in src/api/main.py.
```

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-12-04 | Claude + Joris | Initial MVP PRD |

---

*End of Product Requirements Document*
