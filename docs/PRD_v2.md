# Product Requirements Document
## Favonius Energy — EV Fleet Depot Optimization Platform
### Version 2.2 (MVP) | December 2025

---

## Document Purpose

This PRD serves as the **single source of truth** for building the Favonius MVP. It is structured for:
1. **Human engineers** — Clear problem statements, acceptance criteria, and design decisions
2. **AI coding agents (Cursor, Claude Code)** — Precise specifications, code patterns, and validation rules

**How to use this document with Cursor IDE:**
- Reference sections using `@PRD_v2.md#section-name`
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
| OCPP 1.6/2.0.1 charger control (CCS only) | Mobile app |
| Energy consumption surrogate model | Multi-company deployments |
| Re-optimization triggers | Grid services participation |
| Single-depot operation | Full carbon accounting |
| Inter-depot vehicle handoff messaging | Multi-connector support (CHAdeMO, Type2, NACS) |
| Building load integration | — |

### 1.3 Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Demand charge reduction | ≥ 30% | (Baseline - Optimized) / Baseline |
| Optimization solve time | < 60 seconds | Gurobi wall-clock time (excluding state assembly and dispatch) for 20 vehicles |
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
| **Physical accessibility** | Not all chargers physically reachable by all vehicles | Charger-vehicle assignment respects physical constraints |
| **CCS-only MVP** | Only CCS connector supported in MVP | Simplifies connector compatibility logic |

---

## 3. Solution Overview

### 3.1 Core Value Proposition

A software platform that:
1. **Ingests** real-time data (vehicle SoC, schedules, prices, weather, building load)
2. **Predicts** energy consumption using a surrogate model
3. **Optimizes** charging schedules and battery dispatch via MILP
4. **Controls** chargers through OCPP and batteries through Modbus
5. **Monitors** for conditions requiring re-optimization
6. **Coordinates** inter-depot vehicle handoffs

### 3.1.1 Depot Independence

Each depot runs its own optimization independently. Vehicles can move between depots via handoff messages (Section 5.4), but there is no cross-depot joint optimization layer in MVP. This means:
- Each depot optimizes only its own vehicles and chargers
- Inter-depot coordination is limited to handoff messaging
- No global optimization across multiple depots

### 3.2 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **MILP over RL for dispatch** | Guarantees optimal solution with hard constraints; RL reserved for forecasting |
| **24-hour rolling horizon** | Captures diurnal patterns; aligns with day-ahead price markets |
| **15-minute timestep (Δt)** | Matches demand charge intervals; balances fidelity vs. complexity |
| **Gaussian Process surrogate** | Provides uncertainty estimates; proven in Stanford research (R² 0.84-0.94) |
| **Python + Pyomo + Gurobi** | Production-grade solver with excellent performance and constraint handling |
| **TimescaleDB for telemetry** | Optimized for time-series; compression for long-term storage |
| **OCPP 1.6 primary, 2.0.1 ready** | 1.6 is dominant in field; 2.0.1 adds smart charging profiles |
| **CCS connector only (MVP)** | Simplifies physical constraints; dominant DC fast charging standard |
| **Building load required** | Enables accurate grid power tracking; sellable feature |

### 3.3 MVP Simplifications

| Simplified Area | MVP Approach | Future Enhancement |
|-----------------|--------------|-------------------|
| Solar prediction | Removed from MVP | Add when self-consumption focus needed |
| Price forecasting | Use market/TOU prices directly | Add RL forecaster for volatile markets |
| V2G | Unidirectional only | Add when market revenue justifies |
| Multi-depot | Single depot optimization with handoff messaging | Add coordination layer |
| Connector types | CCS only | Add CHAdeMO, Type2, NACS support |

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
- [ ] Schedule respects physical charger-vehicle accessibility
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
- [ ] Building load included in grid power calculation

#### US-03: Re-optimization on Price Spikes
```
AS A facilities manager
I WANT the system to re-optimize when prices change significantly
SO THAT I can take advantage of price arbitrage opportunities
```

**Acceptance Criteria:**
- [ ] Price change threshold: >25% OR >$25/MWh
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

#### US-05: Vehicle Return Time Deviation Handling
```
AS A fleet operations manager
I WANT the system to adjust schedules if a vehicle returns late
SO THAT charging can be replanned appropriately
```

**Acceptance Criteria:**
- [ ] Return time deviation threshold: >15 minutes
- [ ] Re-optimization triggered within 1 minute of detection
- [ ] New schedule accounts for reduced charging window
- [ ] System logs expected vs actual return times

#### US-06: Inter-depot Vehicle Handoff
```
AS A fleet operations manager
I WANT the destination depot notified when a vehicle is en route
SO THAT charging can be planned before arrival
```

**Acceptance Criteria:**
- [ ] Message sent when vehicle departs origin depot
- [ ] Message includes: vehicle ID, expected SoC, arrival time, battery capacity, max_charge_kw
- [ ] Destination depot incorporates vehicle into next optimization
- [ ] Handoff logged for audit trail
- [ ] Incoming vehicle appears in destination depot state assembly

---

## 5. System Architecture

### 5.1 High-Level Architecture

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
│  • Charger availability (aggregated by rated_kw)                    │
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
│                       (Pyomo + Gurobi)                              │
│                                                                     │
│  Objective: min(Energy Cost + Demand Charges)                       │
│  Decision Variables: P_charge[b,t], P_batt[t], y_charge[b,t]        │
│  Hard Constraint: SoC[b, t_depart] ≥ 99%                            │
│                                                                     │
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
│  Event-driven (SoC/return/handoff) | Periodic (price/scheduled)    │
├─────────────────────────────────────────────────────────────────────┤
│  Trigger                    │ Detection Method │ Threshold           │
│  ─────────────────────────────────────────────────────────────────  │
│  Vehicle SoC deviation      │ Event-driven     │ > 5%                │
│  Vehicle return time        │ Event-driven     │ > 15 minutes late   │
│  Inter-depot handoff        │ Event-driven     │ On message receipt  │
│  Price change               │ On ingestion     │ > 25% OR > $25/MWh   │
│  Scheduled (default)        │ Periodic         │ Hourly 7AM-11PM      │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 Component Responsibilities

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **Weather Adapter** | Fetch 7-day forecast | Open-Meteo API, httpx |
| **Price Adapter** | Fetch TOU/CAISO prices | CAISO OASIS, utility APIs |
| **Building Load Adapter** | Fetch building power consumption | Modbus meter, API, or forecast |
| **OCPP Server** | Charger communication | ocpp library, WebSocket |
| **Surrogate Model** | Energy consumption prediction | scikit-learn, gpytorch |
| **State Assembler** | Aggregate inputs for optimizer | asyncpg, pandas |
| **MILP Optimizer** | Generate optimal schedules | Pyomo, Gurobi |
| **Charger Allocator** | Allocate aggregated power to individual chargers | Post-optimization allocation |
| **Trigger Monitor** | Detect re-optimization conditions | asyncio |
| **Control Dispatcher** | Send commands to hardware | OCPP, Modbus |
| **Handoff Manager** | Send/receive inter-depot messages | HTTP/WebSocket |
| **API Server** | External interface | FastAPI, uvicorn |
| **Database** | Persistent storage | TimescaleDB (PostgreSQL) |

### 5.3 Data Flow

```
1. INGESTION (every 5 minutes)
   Weather API → weather_forecasts table
   CAISO API → prices table
   OCPP MeterValues → telemetry table (includes vehicle max_charge_kw)
   Fleet Mgmt System → schedules table
   Building Load Meter/API → building_load table
   Inter-depot Messages → interdepot_messages table

2. STATE ASSEMBLY (before each optimization)
   Query: latest telemetry, prices, schedules, depot config, building load
   Query: pending inter-depot incoming vehicles (where arrival_time < horizon_end)

   **Data Freshness Requirements:**
   | Data Type | Max Age | Fallback if Stale |
   |-----------|---------|-------------------|
   | Vehicle SoC (telemetry) | 15 minutes | Use last known + log warning |
   | Prices | 24 hours | Use cached TOU schedule |
   | Weather forecast | 6 hours | Use last forecast |
   | Building load | 30 minutes | Use forecast model |
   | Schedules | N/A (static) | Required, fail if missing |

   For each incoming vehicle:
     - Add to vehicle_socs with expected_soc
     - Set availability to False before arrival_time, True after
     - Include in energy_requirements and departure_times if applicable
   Compute: vehicle availability windows, energy requirements
   Compute: charger-vehicle accessibility matrix
   Aggregate: chargers by rated_kw for optimization
   Resolve: demand_charge_rate (prices.demand_kw → depots.demand_charge_rate_kw)
   Output: DepotState object

3. OPTIMIZATION (hourly + triggers)
   Input: DepotState, DepotConfig
   Execute: MILP solve (< 60s)
   Output: Charging schedule, battery dispatch

4. DISPATCH (immediately after optimization)
   Allocate: Aggregated charger power to individual chargers
   OCPP: SetChargingProfile to each charger
   Modbus: Battery setpoints
   Database: Store optimization results

5. MONITORING
   Event-driven triggers (SoC, return time, handoff):
   - SoC deviation: Detected within 15 seconds of receiving new telemetry
   - Return time deviation: Detected within 15 seconds of schedule update
   - Inter-depot handoff: Detected on message receipt
   - Optimization starts within 60 seconds of trigger detection
   
   Periodic triggers (price, scheduled):
   - Price change: Evaluated on each price ingestion event (every 5 minutes)
     If deviation >25% OR >$25/MWh, trigger immediately (subject to rate limiting)
   - Scheduled: Evaluated hourly 7AM-11PM
   - Optimization completes within 60 seconds of trigger

6. INTER-DEPOT COORDINATION (on vehicle departure)
   Send: Handoff message to destination depot
   Receive: Acknowledge and incorporate into next optimization
```

### 5.4 Inter-Depot Handoff Flow

```
┌─────────────┐                              ┌─────────────┐
│   Depot A   │                              │   Depot B   │
│  (Origin)   │                              │(Destination)│
└──────┬──────┘                              └──────┬──────┘
       │                                            │
       │  1. Vehicle bus_1 departs                  │
       │     for Depot B                            │
       ├───────────────────────────────────────────►│
       │  POST /depots/{dest_depot}/handoff/receive │
       │  {vehicle_id, expected_soc: 0.35,          │
       │   arrival_time, battery_kwh, max_charge_kw}│
       │                                            │
       │                              2. Depot B    │
       │                                 stores in  │
       │                                 pending    │
       │                                 arrivals   │
       │                                            │
       │                              3. Next       │
       │                                 optimization│
       │                                 includes   │
       │                                 bus_1      │
       │                                            │
       │  4. Vehicle arrives                        │
       │◄───────────────────────────────────────────┤
       │     OCPP BootNotification                  │
       │                                            │
       │                              5. Depot B    │
       │                                 owns bus_1 │
       │                                 until      │
       │                                 departure  │
       └────────────────────────────────────────────┘
```

**API Flow:**
1. Client (UI or integration) calls `POST /depots/{origin_depot}/vehicles/{vehicle_id}/handoff` on origin depot
2. Origin depot:
   - Writes `interdepot_messages` row with status='pending'
   - Issues HTTP call to `POST /depots/{dest_depot}/handoff/receive` on destination instance
3. Destination depot:
   - Validates request
   - Stores message in `interdepot_messages` with status='acknowledged'
   - Returns acknowledgment with `acknowledged_at` timestamp
4. Origin depot updates message status to 'acknowledged' and stores `acknowledged_at`

---

## 6. Data Models

### 6.1 Database Schema

```sql
-- PostgreSQL + TimescaleDB

-- ============ REFERENCE DATA ============

CREATE TABLE depots (
    depot_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    timezone        VARCHAR(50) DEFAULT 'America/Los_Angeles',
    utility_id      VARCHAR(100),  -- Utility provider identifier (e.g., 'PG&E', 'SCE')
    max_grid_kw     DOUBLE PRECISION NOT NULL,
    demand_charge_rate_kw  DOUBLE PRECISION DEFAULT 20.0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE vehicles (
    vehicle_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    external_id     VARCHAR(100) UNIQUE NOT NULL,  -- customer's vehicle ID (e.g., 'bus_101')
    vehicle_type    VARCHAR(50) NOT NULL,  -- 'bus_large', 'bus_small', 'van'
    battery_kwh     DOUBLE PRECISION NOT NULL,
    max_charge_kw   DOUBLE PRECISION NOT NULL,  -- Default from config, updated by OCPP
    id_tag          VARCHAR(100),  -- OCPP idTag used in Authorize messages to map sessions to vehicles
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE chargers (
    charger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    ocpp_id         VARCHAR(100) UNIQUE NOT NULL,
    rated_kw        DOUBLE PRECISION NOT NULL,
    efficiency      DOUBLE PRECISION DEFAULT 0.95,
    connector_type  VARCHAR(50) DEFAULT 'CCS',  -- MVP: CCS only
    status          VARCHAR(20) DEFAULT 'Available',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Physical accessibility: which vehicles can use which chargers
CREATE TABLE charger_vehicle_access (
    charger_id      UUID NOT NULL REFERENCES chargers(charger_id),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    is_accessible   BOOLEAN DEFAULT TRUE,
    notes           VARCHAR(255),  -- e.g., "bay 3 blocked by pillar"
    PRIMARY KEY (charger_id, vehicle_id)
);

CREATE TABLE battery_storage (
    battery_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    capacity_kwh    DOUBLE PRECISION NOT NULL CHECK (capacity_kwh > 0),
    max_power_kw    DOUBLE PRECISION NOT NULL CHECK (max_power_kw > 0),
    efficiency      DOUBLE PRECISION DEFAULT 0.92 CHECK (efficiency > 0 AND efficiency <= 1),
    soc_min         DOUBLE PRECISION DEFAULT 0.2 CHECK (soc_min >= 0 AND soc_min < 1),
    soc_max         DOUBLE PRECISION DEFAULT 0.8 CHECK (soc_max > 0 AND soc_max <= 1),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT battery_soc_range CHECK (soc_min < soc_max)
);

-- ============ TIME-SERIES DATA ============

CREATE TABLE telemetry (
    time            TIMESTAMPTZ NOT NULL,
    vehicle_id      UUID NOT NULL,
    charger_id      UUID REFERENCES chargers(charger_id),  -- Charger that reported this telemetry
    soc             DOUBLE PRECISION CHECK (soc >= 0 AND soc <= 1),  -- 0.0 to 1.0
    location_lat    DOUBLE PRECISION CHECK (location_lat >= -90 AND location_lat <= 90),
    location_lon    DOUBLE PRECISION CHECK (location_lon >= -180 AND location_lon <= 180),
    is_plugged      BOOLEAN,
    charging_kw     DOUBLE PRECISION CHECK (charging_kw >= 0),
    odometer_km     DOUBLE PRECISION CHECK (odometer_km >= 0),
    max_charge_kw   DOUBLE PRECISION CHECK (max_charge_kw > 0)  -- From OCPP MeterValues
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

CREATE TABLE building_load (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    power_kw        DOUBLE PRECISION NOT NULL,  -- Building load (kW)
    source          VARCHAR(50) NOT NULL,       -- 'meter', 'api', 'forecast'
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('building_load', 'time');

-- ============ OPERATIONAL DATA ============

CREATE TABLE schedules (
    schedule_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    route_id        VARCHAR(100),
    departure_time  TIMESTAMPTZ NOT NULL,
    return_time     TIMESTAMPTZ NOT NULL,
    actual_return_time TIMESTAMPTZ,  -- Updated when vehicle actually returns
    energy_kwh      DOUBLE PRECISION,  -- Estimated energy consumption (kWh) from surrogate model
    required_soc    DOUBLE PRECISION DEFAULT 1.0,
    dest_depot_id   UUID REFERENCES depots(depot_id),  -- if different
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_schedules_vehicle_depart ON schedules (vehicle_id, departure_time);

CREATE TABLE optimization_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    run_time        TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  VARCHAR(50) NOT NULL,  -- 'scheduled', 'price_spike', 'soc_deviation', 'return_time_deviation', 'interdepot_handoff'
    horizon_start   TIMESTAMPTZ NOT NULL,
    horizon_end     TIMESTAMPTZ NOT NULL,
    solve_time_s    DOUBLE PRECISION,
    objective_value DOUBLE PRECISION,
    peak_demand_kw  DOUBLE PRECISION,
    status          VARCHAR(20) DEFAULT 'completed',
    schedule_json   JSONB NOT NULL  -- Full optimization schedule (includes solver_used metadata)
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
    expected_soc    DOUBLE PRECISION NOT NULL CHECK (expected_soc >= 0 AND expected_soc <= 1),
    arrival_time    TIMESTAMPTZ NOT NULL,
    battery_kwh     DOUBLE PRECISION NOT NULL CHECK (battery_kwh > 0),
    max_charge_kw   DOUBLE PRECISION NOT NULL CHECK (max_charge_kw > 0),
    status          VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'acknowledged', 'arrived')),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    arrived_at      TIMESTAMPTZ,
    CONSTRAINT valid_depot_pair CHECK (origin_depot_id != dest_depot_id),
    CONSTRAINT valid_timing CHECK (arrival_time > departure_time)
);
CREATE INDEX idx_interdepot_dest_status ON interdepot_messages (dest_depot_id, status);

CREATE TABLE trigger_log (
    trigger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    trigger_type    VARCHAR(50) NOT NULL,  -- 'soc_deviation', 'price_change', 'return_time_deviation', 'interdepot_handoff', 'scheduled'
    trigger_time    TIMESTAMPTZ DEFAULT NOW(),
    details         JSONB,  -- e.g., {vehicle_id, expected_soc, actual_soc, deviation}
    run_id          UUID REFERENCES optimization_runs(run_id)  -- Resulting optimization run
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
    utility_id: Optional[str]
    max_grid_kw: float
    demand_charge_rate_kw: float


@dataclass
class Vehicle:
    vehicle_id: UUID
    depot_id: UUID
    external_id: str
    vehicle_type: str  # 'bus_large', 'bus_small', 'van'
    battery_kwh: float
    max_charge_kw: float  # Default from config, can be overridden by OCPP
    id_tag: Optional[str] = None  # OCPP idTag used in Authorize messages


@dataclass
class Charger:
    charger_id: UUID
    depot_id: UUID
    ocpp_id: str
    rated_kw: float
    efficiency: float = 0.95
    connector_type: str = 'CCS'  # MVP: CCS only
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
    actual_return_time: Optional[datetime] = None


@dataclass
class IncomingVehicle:
    """Vehicle arriving from another depot via handoff."""
    vehicle_id: UUID
    external_id: str
    expected_soc: float
    arrival_time: datetime
    battery_kwh: float
    max_charge_kw: float
    origin_depot_id: UUID


@dataclass
class DepotConfig:
    """Static configuration for optimization.
    
    Note: Chargers are aggregated by rated power for optimization to reduce
    variable count. Individual chargers are stored in database, but optimization
    treats chargers of the same rated_kw as a single aggregated resource.
    After optimization, power is allocated back to individual chargers.
    """
    vehicle_capacities: dict[str, float]      # vehicle_id -> kWh
    vehicle_max_charge_kw: dict[str, float]   # vehicle_id -> max charge rate (kW)
    charger_groups: dict[float, int]          # rated_kw -> count of chargers with that rating
    charger_efficiency: float                 # Assumed uniform across all chargers
    charger_vehicle_access: dict[str, set[str]]  # charger_id -> set of accessible vehicle_ids
    battery_capacity: float                   # kWh (0 if no battery)
    battery_power: float                      # kW (0 if no battery)
    battery_efficiency: float = 0.92          # Round-trip efficiency for stationary battery
    battery_soc_min: float = 0.2
    battery_soc_max: float = 0.8
    max_site_power: float = 1000.0            # kW
    delta_t: float = 0.25                     # hours (15 min)
    n_timesteps: int = 96                     # 24 hours


@dataclass
class DepotState:
    """Dynamic state for optimization."""
    vehicle_socs: dict[str, float]            # vehicle_id -> SoC [0,1]
    battery_soc: float                        # Stationary battery SoC [0,1]
    prices: list[float]                       # $/kWh per timestep
    demand_charge_rate: float                 # $/kW (resolved per Section 8.1 precedence rules)
    current_month_peak: float                 # kW (moving limit)
    vehicle_availability: dict[str, list[bool]]  # vehicle_id -> availability per timestep
    energy_requirements: dict[str, float]     # vehicle_id -> kWh needed for next trip
    departure_times: dict[str, int]           # vehicle_id -> timestep index of departure
    building_power: list[float]               # Building load per timestep (kW)
    incoming_vehicles: list[IncomingVehicle] = field(default_factory=list)  # Inter-depot arrivals


@dataclass
class OptimizationResult:
    """Output from optimization."""
    run_id: UUID
    schedule: dict[str, dict]  # vehicle_id -> {charging_power: [], soc: []}
    battery_dispatch: list[float]  # Battery power per timestep (+discharge, -charge)
    grid_power: list[float]  # Grid power per timestep
    peak_demand_kw: float  # Maximum grid power (kW)
    objective_value: float
    solve_time_s: float
    status: str  # 'optimal', 'feasible', 'degraded', 'infeasible', 'timeout'
    solver_used: str = 'gurobi'  # 'gurobi' or 'highs' - tracks which solver was used for monitoring
    # 'degraded' = solved with relaxed constraints (see Section 8.5.1)
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
    "status": "optimal",
    "objective_value": 1234.56,
    "solve_time_s": 12.3,
    "peak_demand_kw": 450.0,
    "schedule": {
        "bus_101": {
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
        "bus_101": 0.45,
        "bus_102": 0.82
    },
    "battery_soc": 0.55,
    "current_month_peak_kw": 380.0,
    "current_price_kwh": 0.15,
    "building_load_kw": 45.0,
    "incoming_vehicles": [
        {
            "vehicle_id": "uuid",
            "external_id": "bus_201",
            "expected_soc": 0.35,
            "arrival_time": "2025-12-04T14:30:00Z"
        }
    ]
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
    "arrival_time": "2025-12-04T14:30:00Z",
    "battery_kwh": 324.0,
    "max_charge_kw": 150.0
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

#### POST /depots/{depot_id}/handoff/receive
Receive inter-depot handoff message (called by origin depot).

**Request:**
```json
{
    "message_id": "uuid",
    "origin_depot_id": "uuid",
    "vehicle_id": "uuid",
    "external_id": "bus_201",
    "expected_soc": 0.35,
    "arrival_time": "2025-12-04T14:30:00Z",
    "battery_kwh": 324.0,
    "max_charge_kw": 150.0
}
```

**Response:**
```json
{
    "status": "acknowledged",
    "acknowledged_at": "2025-12-04T10:00:05Z"
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
        "ocpp_server": "healthy",
        "gurobi_license": "valid"
    }
}
```

---

### 7.2 WebSocket API (OCPP)

The platform implements an OCPP 1.6 Central System at `ws://<host>:9000/{ocpp_id}`.

**Supported Messages:**
| Direction | Message | Purpose |
|-----------|---------|---------|
| CP → CS | BootNotification | Charger registration |
| CP → CS | StatusNotification | Charger status updates |
| CP → CS | MeterValues | Energy, SoC, and max_charge_kw readings |
| CP → CS | StartTransaction | Charging session start |
| CP → CS | StopTransaction | Charging session end |
| CS → CP | SetChargingProfile | Dispatch charging schedule |
| CS → CP | RemoteStartTransaction | Initiate charging |
| CS → CP | RemoteStopTransaction | Stop charging |

**MeterValues Processing:**
When MeterValues include vehicle max charge rate (from smart charging capable chargers), this value is:
1. Stored in telemetry table with timestamp
2. Used to update vehicle's effective max_charge_kw for optimization
3. Takes precedence over static config value

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

---

## 8. Optimization Engine Specifications

### 8.1 Mathematical Formulation

**Objective Function:**
```
minimize:
    Σ_t (price[t] × P_grid[t] × Δt)           # Energy cost
  + demand_charge_rate × P_max_grid           # Demand charge
```

**Decision Variables:**
| Variable | Domain | Description |
|----------|--------|-------------|
| P_charge[b,t] | ℝ≥0 | Charging power for vehicle b at time t (kW) |
| SoC[b,t] | [0.0, 1.0] | State of charge for vehicle b at time t |
| y_charge[b,t] | {0,1} | Binary: is vehicle b charging at time t |
| P_batt[t] | [-P_batt_max, P_batt_max] | Battery power (+discharge, -charge) |
| SoC_batt[t] | [soc_min, soc_max] | Battery state of charge |
| P_grid[t] | ℝ≥0 | Grid power draw at time t |
| P_max_grid | ℝ≥0 | Maximum grid power (demand) |

**Power Limits and Demand Charge:**
- `max_site_power` (sourced from `depots.max_grid_kw`): Physical site interconnection limit (kW)
  Constraint: P_grid[t] ≤ max_site_power   ∀t

- `P_max_grid`: Billing demand for the month (decision variable, kW)
  Constraints:
    P_max_grid ≥ P_grid[t]   ∀t in horizon
    P_max_grid ≥ current_month_peak   (moving limit from current month)
  Objective component: demand_charge_rate × P_max_grid

**Demand Charge Rate Resolution:**
The demand charge rate used in optimization is resolved in this priority order:
1. If `prices.demand_kw` is not null (from most recent price row), use that value
2. Otherwise, use `depots.demand_charge_rate_kw`
3. The resolved value is stored in `DepotState.demand_charge_rate`

**Constraints:**

1. **SoC Initialization:**
   ```
   For existing depot vehicles:
   SoC[b, 0] = current_measured_soc[b]   ∀b in depot_vehicles

   Where current_measured_soc[b] is the latest telemetry value, clamped to [0.0, 1.0]

   For incoming vehicles (from inter-depot handoff):
   SoC[b, t] is unconstrained for t < t_arrive[b]  (vehicle not yet at depot)
   SoC[b, t_arrive[b]] = expected_soc[b]           (fixed at arrival)

   See Constraint 12 for full incoming vehicle handling.
   ```

2. **SoC Dynamics:**
   ```
   SoC[b,t] = SoC[b,t-1] + (η × P_charge[b,t-1] × Δt) / E_batt[b]   ∀b, t > 0
   
   Where:
   - η (eta) is the charging efficiency (0.90-0.95)
   - E_batt[b] is the usable battery capacity for vehicle b (kWh)
   ```

3. **SoC Bounds:**
   ```
   0.0 ≤ SoC[b,t] ≤ 1.0   ∀b,t
   
   Note: Input SoC from telemetry is clamped to [0.0, 1.0] before optimization.
   A tiny positive floor (e.g., 0.001) may be added in implementation to avoid
   numerical issues, but the constraint domain is [0.0, 1.0].
   ```

4. **Vehicle Availability:**
   ```
   P_charge[b,t] = 0   ∀b, t where available[b,t] = False
   
   Implemented as: For each vehicle b and timestep t, if the vehicle
   is not available (on route, not at depot), then P_charge[b,t] = 0.
   ```

5. **Departure SoC (HARD CONSTRAINT):**
   ```
   SoC[b, t_depart[b]] ≥ 0.99   ∀b with scheduled departure
   ```

6. **Charger Linking (vehicle max charge rate):**
   ```
   P_charge[b,t] ≤ max_charge_kw[b] × y_charge[b,t]
   
   Where max_charge_kw[b] is the effective max charge rate:
   - Resolved per Section 8.4 (OCPP → config → charger cap)
   - Already capped at charger rated_kw in resolution step
   ```

7. **Charger Capacity (Aggregated by Power and Count):**
   ```
   Power limit:
   Σ_b P_charge[b,t] ≤ Σ_g (n_chargers[g] × P_g)   ∀t

   Vehicle count limit:
   Σ_b y_charge[b,t] ≤ Σ_g n_chargers[g]   ∀t

   Where:
   - g indexes charger groups (grouped by rated_kw)
   - n_chargers[g] is the count of chargers in group g
   - P_g is the rated power (kW) for group g
   - y_charge[b,t] is binary: 1 if vehicle b is charging at time t

   Both constraints are required: the power limit prevents exceeding total capacity,
   while the count limit prevents more vehicles charging than available chargers.

   Note: For MVP, the MILP does not enforce per-charger physical accessibility.
   Physical accessibility is enforced deterministically in the post-optimization
   allocation phase (Section 8.3). Acceptance criteria around "physical
   accessibility" refer to actual OCPP commands sent, not the raw MILP solution.
   ```

8. **Grid Power Balance:**
   ```
   P_grid[t] = Σ_b P_charge[b,t] + P_building[t] - P_batt_effective[t]

   Where:
   - P_building[t] is the building load at timestep t (kW)
   - P_batt_effective[t] is the grid-side battery power (see Constraint 11 for efficiency handling)

   For MVP simplification, if efficiency is omitted:
   P_grid[t] = Σ_b P_charge[b,t] + P_building[t] - P_batt[t]
   ```

9. **Site Power Limit:**
   ```
   P_grid[t] ≤ max_site_power   ∀t
   
   Where max_site_power is sourced from depots.max_grid_kw
   ```

10. **Demand Tracking:**
    ```
    P_max_grid ≥ P_grid[t]   ∀t
    P_max_grid ≥ current_month_peak   (moving limit)
    
    Where:
    - P_max_grid is the billing demand decision variable
    - current_month_peak is the maximum grid power observed this month (kW)
    
    Note: For MVP, we assume 15-minute demand charge periods (matching optimization timestep Δt).
    Future versions will support configurable billing periods (15-min, 30-min) and track maximum
    demand per period separately.
    ```

11. **Battery Dynamics:**
    ```
    SoC_batt[0] = current_battery_soc   (initial condition)

    SoC_batt[t] = SoC_batt[t-1] - (P_batt[t-1] × Δt) / E_batt_storage   ∀t > 0
    soc_min ≤ SoC_batt[t] ≤ soc_max   ∀t

    Where:
    - P_batt[t] > 0 means discharging (reducing SoC)
    - P_batt[t] < 0 means charging (increasing SoC)
    - E_batt_storage is the battery capacity (kWh)

    Efficiency Handling:
    For MVP, round-trip efficiency (η_batt, typically 0.92 from DepotConfig.battery_efficiency)
    is modeled as a power loss during discharge only:
    - Grid receives: P_batt × η_batt when P_batt > 0 (discharge)
    - Grid provides: |P_batt| when P_batt < 0 (charge)

    This means the grid power balance (Constraint 8) should use:
    P_grid[t] = Σ_b P_charge[b,t] + P_building[t] - P_batt_effective[t]

    Where P_batt_effective[t] = P_batt[t] × η_batt if P_batt[t] > 0, else P_batt[t]

    Note: For MVP simplification, implementations may omit efficiency entirely from
    battery dynamics and note this as a modeling limitation. Future versions will
    model charge/discharge efficiencies separately for higher accuracy.
    ```

12. **Incoming Vehicle Availability:**
    ```
    For each incoming vehicle i with arrival_time t_arrive:
    - If t_arrive < horizon_end:
      - SoC[i, t_arrive] = expected_soc[i]  (fixed at arrival)
      - available[i, t] = False   ∀t < t_arrive
      - available[i, t] = True   ∀t ≥ t_arrive
      - Include in departure SoC constraint (Constraint 5) if departure within horizon
    ```

### 8.2 Solver Configuration

**Primary Solver: Gurobi**
```python
# Pyomo + Gurobi configuration
import pyomo.environ as pyo

solver = pyo.SolverFactory('gurobi')

# Time limit: 60 seconds for Gurobi solve time only
# Total optimization latency (state assembly + solve + dispatch) target: < 60 seconds
# If solve time consistently exceeds 45 seconds, reduce TimeLimit to ensure total < 60s
solver.options['TimeLimit'] = 60

# MIP optimality gap: 1% acceptable for production
solver.options['MIPGap'] = 0.01

# Thread count: adjust based on deployment environment
solver.options['Threads'] = 4

# Aggressive presolve for faster solve times
solver.options['Presolve'] = 2  # 0=off, 1=conservative, 2=aggressive

# Numerical stability for energy models
solver.options['NumericFocus'] = 3  # 0=default, 3=highest accuracy

# Logging: 1=normal output
solver.options['OutputFlag'] = 1

# Warm start from previous solution
solver.options['WarmStart'] = 1

# Note: Gurobi requires valid license. Check license status in health endpoint.
```

**Fallback Solver: HiGHS (Open-Source)**

For reliability and fault tolerance, the system MUST support automatic fallback to HiGHS if Gurobi is unavailable (license failure, connection issues, or solver errors). This ensures graceful degradation rather than complete system failure.

```python
# Fallback solver configuration (HiGHS via appsi_highs)
fallback_solver = pyo.SolverFactory('appsi_highs')

# Time limit: Same 60 seconds
fallback_solver.options['time_limit'] = 60

# MIP optimality gap: 1% (same target)
fallback_solver.options['mip_rel_gap'] = 0.01

# Thread count: adjust based on deployment environment
fallback_solver.options['threads'] = 4

# Presolve: enabled
fallback_solver.options['presolve'] = 'on'

# Note: HiGHS is open-source and does not require a license.
# Performance may be slower than Gurobi, but provides reliable fallback.
```

**Solver Selection Logic:**

1. **Primary attempt**: Try Gurobi solver
   - Check license validity before solving
   - If license invalid or solver unavailable, log warning and fall back

2. **Fallback attempt**: If Gurobi fails, automatically use HiGHS
   - Log fallback event with reason (license failure, connection error, etc.)
   - Mark optimization result with `solver_used: 'highs'` in metadata
   - Continue with same time limit and gap targets

3. **Error handling**: If both solvers fail, return `status: 'error'` with detailed error message

**Implementation Requirements:**
- Solver selection must be transparent to optimization logic (same interface)
- Fallback must be automatic (no manual intervention required)
- All solver failures must be logged for monitoring
- Health endpoint must report solver availability (Gurobi license status, HiGHS availability)
- Optimization results must include `solver_used` field for analysis

### 8.3 Charger Aggregation Strategy

To reduce optimization complexity, chargers are aggregated by `rated_kw` before optimization:

1. **Pre-optimization**: Group chargers by `rated_kw` into charger groups
   - Example: 5 chargers @ 50kW, 3 chargers @ 80kW → groups: {50: 5, 80: 3}
   - Optimization uses aggregated groups, reducing binary variables

2. **Optimization**: Constraint uses total charger capacity per group
   - `Σ_b y_charge[b,t] ≤ Σ_g n_chargers[g]` where g indexes charger groups

3. **Post-optimization**: Allocate power to individual chargers
   - For each timestep and charger group:
     - Sort vehicles by priority (departure time, SoC deficit)
     - Check physical accessibility (charger_vehicle_access)
     - Allocate power to chargers within group using fair allocation
     - Respect individual charger `rated_kw` limits
     - Handle charger status (Available/Unavailable)

**Post-optimization Allocation Algorithm:**
For each timestep t and charger group g:
1. Get vehicles assigned to group g by optimizer (where P_charge[b,t] > 0)
2. Sort vehicles by priority: (departure_time ASC, SoC_deficit DESC)
3. For each vehicle in priority order:
   - Find accessible chargers in group g (via charger_vehicle_access table)
   - Allocate power to first available charger up to min(vehicle_max_kw, charger_rated_kw)
   - If vehicle needs more power, allocate to next accessible charger
   - Continue until vehicle power is fully allocated or no accessible chargers remain
4. Respect individual charger `rated_kw` limits
5. Skip chargers with status != 'Available'
6. If allocation fails (no accessible chargers), log warning and use fallback allocation

**Benefits:**
- Reduces MILP variables from O(n_chargers × n_vehicles × n_timesteps) to O(n_groups × n_vehicles × n_timesteps)
- Faster solve times while maintaining optimality
- Individual charger constraints still enforced in allocation phase

### 8.4 Vehicle Max Charge Rate Resolution

Vehicle charging rate limits are resolved in this priority order:

1. **OCPP MeterValues** (highest priority): If the charger reports vehicle max charge rate via MeterValues within the last 15 minutes, use this value
2. **Vehicle config** (fallback): Use `max_charge_kw` from vehicles table
3. **Charger rated_kw** (cap): Never exceed the charger's rated power

```python
def get_effective_max_charge_kw(vehicle_id: str, charger_rated_kw: float, 
                                 telemetry_max_kw: Optional[float],
                                 config_max_kw: float) -> float:
    """Determine effective max charge rate for a vehicle."""
    if telemetry_max_kw is not None:
        vehicle_max = telemetry_max_kw
    else:
        vehicle_max = config_max_kw
    
    return min(vehicle_max, charger_rated_kw)
```

### 8.5 Performance Targets

| Metric | Target | Test Configuration |
|--------|--------|-------------------|
| Solve time | < 60 seconds | 20 vehicles, 96 timesteps |
| Memory | < 2 GB | Same configuration |
| Optimality gap | < 1% | Required for deployment |
| Warm-start speedup | > 3x | Use previous solution |

### 8.5.1 Infeasibility Handling

When optimization cannot satisfy all constraints (typically: insufficient time to charge a vehicle to 99% SoC):

**Detection:**
- Gurobi returns `TerminationCondition.infeasible`
- Log infeasibility with depot_id, trigger_reason, and constraint analysis

**Response Strategy (in priority order):**

1. **Identify conflicting vehicles:**
   - Use Gurobi's IIS (Irreducible Inconsistent Subsystem) to identify which departure constraints cannot be met
   - Log affected vehicle_ids with expected vs achievable SoC

2. **Relaxed solve (fallback):**
   - Relax departure SoC from 99% to 90% for affected vehicles only
   - Re-solve with relaxed constraints
   - Mark result as `status: 'degraded'`

3. **Alert generation:**
   - Create high-priority alert for operations team
   - Include: affected vehicles, expected SoC shortfall, departure times
   - Suggest: delay departure, pre-position vehicle at charger, manual override

4. **Never silently fail:**
   - Always return a schedule (even if degraded)
   - `OptimizationResult.status` must be one of: 'optimal', 'feasible', 'degraded', 'infeasible'
   - 'infeasible' only returned if even relaxed solve fails

**Logging:**
```json
{
  "event": "optimization_infeasible",
  "depot_id": "uuid",
  "trigger_reason": "scheduled",
  "affected_vehicles": [
    {"vehicle_id": "bus_101", "target_soc": 0.99, "achievable_soc": 0.85, "departure_time": "2025-12-04T06:00:00Z"}
  ],
  "action_taken": "relaxed_solve",
  "relaxed_target_soc": 0.90,
  "result_status": "degraded"
}
```

### 8.6 Surrogate Model Specification

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
- R² ≥ 0.85 on validation set (applies to whichever model is active: GP or MLP fallback)
- Uncertainty estimates for robust optimization

---

## 9. Integration Requirements

### 9.1 OCPP Integration

**Supported Versions:** OCPP 1.6-J (primary), OCPP 2.0.1 (ready)

**Connector Types (MVP):** CCS only

**Connection Flow:**
```
1. Charger connects: ws://platform:9000/{ocpp_id}
2. Platform sends: BootNotificationResponse (Accepted, interval=300)
3. Charger sends: StatusNotification (every 5 min)
4. Charger sends: MeterValues (every 15 sec when charging)
   - Includes: Energy.Active.Import.Register, SoC, Power.Active.Import
   - May include: maxChargingRate (vehicle limit)
5. Platform sends: SetChargingProfile (after optimization)
```

**Vehicle-Charger Mapping:**
When a StartTransaction is received:
1. Map OCPP `idTag` to `vehicle_id`:
   - First, try matching `idTag` to `vehicles.id_tag` (if field exists)
   - Otherwise, match `idTag` to `vehicles.external_id`
2. Store the associated `charger_id` (from charge point identifier) in all telemetry entries for that transaction
3. When MeterValues include `maxChargingRate`:
   - Store in telemetry table with both `vehicle_id` and `charger_id`
   - Update vehicle's effective max_charge_kw for optimization (per Section 8.4)

### 9.2 Weather API (Open-Meteo)

**Endpoint:** `https://api.open-meteo.com/v1/forecast`

**Parameters:**
- latitude, longitude: Depot location
- daily: temperature_2m_max, temperature_2m_min, precipitation_sum, shortwave_radiation_sum
- forecast_days: 2

**Rate Limits:** 10,000 requests/day (free tier)

### 9.3 Price Data

**CAISO OASIS (California):**
- Endpoint: `http://oasis.caiso.com/oasisapi/SingleZip`
- Data: Day-Ahead LMP by node
- Update: Daily at 10:00 AM PT

**Utility TOU (Fallback):**
- PG&E E-19: Peak (4-9 PM), Partial-Peak (9 AM-4 PM, 9 PM-12 AM), Off-Peak
- Store in depot configuration

### 9.4 Building Load Integration

**Data Sources:**
1. **Modbus Meter** (preferred): Direct connection to building power meter
2. **Building Management System API**: Integration with BMS/SCADA
3. **Forecast Model**: Historical patterns + calendar events (fallback)

**Data Format:**
- Time-series: `(timestamp, power_kw)` at 15-minute intervals
- Forecast horizon: 24 hours ahead
- Update frequency: Every 15 minutes (real-time) or hourly (forecast)

**Storage:**
- Stored in `building_load` hypertable
- Used directly in optimization grid power balance constraint

**Behavior:**
- Building load is REQUIRED for accurate grid power calculation
- If meter unavailable, use forecast model (degraded mode)
- If no forecast available, assume 0 kW and log critical warning
- Optimization will still run but with reduced accuracy
- All building load sources are logged for audit trail

### 9.5 Fleet Management System

**Expected Input Format:**
```json
{
    "schedules": [
        {
            "vehicle_id": "bus_101",
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
| Optimization latency | < 60 seconds | 95th percentile |
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
| API authentication | JWT tokens with expiration (1 hour access, 24 hour refresh) |
| OCPP authentication | Basic auth + TLS 1.3 (wss:// required in production) |
| Database access | Role-based, encrypted connections (SSL required) |
| Secrets management | Environment variables, Vault (future) |
| Audit logging | All API calls, optimization runs, handoff messages |
| Transport security | HTTPS required for all API endpoints in production |
| Inter-depot auth | Mutual TLS or signed JWT for handoff messages |

**Input Validation Requirements:**

| Endpoint/Field | Validation |
|----------------|------------|
| All UUIDs | Valid UUID v4 format |
| SoC values | Range [0.0, 1.0], reject out-of-bounds |
| Power values (kW) | Non-negative, ≤ max_site_power |
| Timestamps | ISO 8601 format, within reasonable range (±30 days) |
| depot_id in path | Must match authenticated user's depot access |
| vehicle_id | Must belong to specified depot |
| Handoff requests | Origin depot must own the vehicle |

**Rate Limiting:**

| Resource | Limit | Window |
|----------|-------|--------|
| API endpoints (general) | 100 requests | per minute |
| POST /optimize | 10 requests | per minute |
| Trigger-induced optimizations | 1 optimization | per 5 minutes per depot |
| Inter-depot handoff | 50 messages | per hour per depot pair |
| Failed auth attempts | 5 attempts | per 15 minutes, then lockout |

**SQL Injection Prevention:**
- All database queries use parameterized statements (asyncpg $1, $2 syntax)
- No string concatenation for SQL queries
- ORM/query builder validates field names against schema

**OCPP Security:**
- Charger ocpp_id validated against registered chargers
- Unregistered chargers rejected at BootNotification
- SetChargingProfile only sent to chargers in 'Available' or 'Charging' status

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
AND solve time (Gurobi wall-clock) is ≤ 45 seconds
AND total optimization latency (state assembly + solve + dispatch) is < 60 seconds
AND charging schedule is dispatched to chargers
```

#### AT-02: Demand Charge Reduction
```gherkin
GIVEN a depot with 200 kW current month peak
AND unmanaged charging would cause 300 kW peak
AND building load averages 50 kW
WHEN optimization runs for a 24-hour horizon
THEN optimized peak is ≤ 220 kW
AND demand charge savings ≥ $1,600/month (at $20/kW)
```

#### AT-03: Price Spike Re-optimization
```gherkin
GIVEN an active charging schedule
AND prices increase from $0.10/kWh to $0.15/kWh (50% increase, +$50/MWh)
WHEN the price trigger fires (>25% OR >$25/MWh)
THEN re-optimization completes (state assembly + solve + dispatch) within 60 seconds of trigger
AND new schedule shifts charging away from high-price period
```

#### AT-04: SoC Deviation Handling
```gherkin
GIVEN bus_101 expected SoC = 0.60 at 2:00 PM
AND actual SoC = 0.52 (8% deviation, > 5% threshold)
WHEN trigger monitor detects deviation
THEN re-optimization is triggered
AND new schedule prioritizes bus_101 charging
AND bus_101 still meets departure requirement
```

#### AT-05: Return Time Deviation Handling
```gherkin
GIVEN bus_101 expected return time = 2:00 PM
AND actual return time = 2:30 PM (30 min late, > 15 min threshold)
WHEN trigger monitor detects deviation
THEN re-optimization is triggered within 1 minute
AND new schedule accounts for reduced charging window
AND bus_101 still meets departure requirement if feasible
```

#### AT-06: Inter-Depot Handoff
```gherkin
GIVEN bus_101 departing depot_A for depot_B
AND expected arrival SoC = 0.35
WHEN bus_101 departs depot_A
THEN depot_B receives handoff message within 30 seconds
AND depot_B's next optimization includes bus_101
AND handoff message includes battery_kwh and max_charge_kw
```

#### AT-07: Building Load Integration
```gherkin
GIVEN a depot with building load averaging 50 kW
AND building load peaks at 80 kW during morning hours
WHEN optimization runs
THEN grid power calculation includes building load
AND peak demand accounts for both charging and building load
```

### 11.2 Unit Test Requirements

| Module | Coverage Target | Critical Paths |
|--------|-----------------|----------------|
| Optimizer | ≥ 90% | Constraint satisfaction, objective calculation |
| Surrogate Model | ≥ 90% | Feature engineering, prediction |
| State Assembler | ≥ 90% | Data aggregation, availability computation, incoming vehicles |
| OCPP Adapter | ≥ 90% | Message handling, profile dispatch, max_charge_kw extraction |
| Trigger Monitor | ≥ 90% | All trigger conditions including return time deviation |
| Handoff Manager | ≥ 90% | Message send/receive, acknowledgment |

### 11.3 Integration Test Requirements

| Test Case | Expected Behavior |
|-----------|-------------------|
| Full optimization cycle | State → Optimize → Dispatch → Verify |
| OCPP charger simulation | Connect → MeterValues → SetChargingProfile |
| Database failover | Graceful degradation, no data loss |
| Price feed failure | Fallback to cached prices |
| Weather API timeout | Use last known forecast |
| Building load meter failure | Use forecast model, log warning |
| Inter-depot handoff | Send → Acknowledge → Include in optimization |

---

## 12. Glossary

- **CAISO**: *California Independent System Operator* — manages California's electricity grid.
- **CCS**: *Combined Charging System* — DC fast charging standard (MVP-supported connector).
- **DAM**: *Day-Ahead Market* — electricity market clearing the day before energy delivery.
- **Demand Charge**: Monthly fee based on peak power draw ($/kW).
- **Δt**: Optimization timestep (15 minutes = 0.25 hours).
- **GP**: *Gaussian Process* — probabilistic machine learning model for energy consumption surrogate.
- **Gurobi**: Commercial MILP solver used for optimization.
- **Horizon**: Planning window for optimization (24 hours).
- **LMP**: *Locational Marginal Price* — spot electricity price at a grid node.
- **MILP**: *Mixed-Integer Linear Programming* — optimization with integer and continuous variables.
- **MPC**: *Model Predictive Control* — rolling horizon optimization approach.
- **OCPP**: *Open Charge Point Protocol* — standard for EV charger communications.
- **Pyomo**: Python optimization modeling library.
- **SoC**: *State of Charge* — battery charge level (0.00-1.00).
- **TOU**: *Time-of-Use* — electricity rate structure varying by time of day.
- **V2G**: *Vehicle-to-Grid* — bidirectional EV charging (post-MVP).
- **Warm-start**: Initializing solver with previous solution for faster convergence.

---

## Appendix A: Cursor IDE Integration

### A.1 Project Rules (.cursorrules)

```
You are a senior Python developer specializing in energy systems optimization.

Project: Favonius Energy - EV Fleet Depot Optimization Platform
Tech Stack: Python 3.12, Pyomo, Gurobi, FastAPI, TimescaleDB, OCPP

Key Patterns:
- Use dataclasses for data models (see src/core/models.py)
- Use asyncpg for database operations
- Use Pyomo for optimization modeling with Gurobi solver
- Follow the Stanford CarbonFree paper approach for surrogate models

Critical Constraints:
- Vehicle departure SoC ≥ 99% is a HARD constraint (never relax)
- Optimization solve time MUST be < 60 seconds
- OCPP 1.6 is primary protocol version
- CCS is the only supported connector type for MVP
- Building load is REQUIRED in grid power calculation

When implementing optimization:
- Reference PRD_v2.md#8-optimization-engine-specifications for formulation
- Use Gurobi solver with specified options (TimeLimit=60, MIPGap=0.01)
- Use warm-starting from previous solutions
- Log solve time and objective value
- Vehicle max_charge_kw comes from OCPP or config (see Section 8.4)

When implementing API endpoints:
- Reference PRD_v2.md#7-api-specifications for contracts
- Return consistent error formats
- Include request/response logging

When implementing triggers:
- Price trigger: >25% OR >$25/MWh
- SoC deviation trigger: >5%
- Return time deviation trigger: >15 minutes

Code Style:
- Type hints required on all functions
- Docstrings in Google format
- Max line length: 100
```

### A.2 Context Files

Place these in your Cursor project for automatic context:

1. `docs/PRD_v2.md` — This document
2. `docs/ARCHITECTURE.md` — System architecture diagram
3. `docs/API.md` — OpenAPI specification
4. `src/core/models.py` — Data models
5. `.cursorrules` — Project rules

### A.3 Recommended Prompts

**For implementing a new feature:**
```
Implement US-02 (Demand Charge Reduction) following the specifications in @PRD_v2.md#4-user-stories--use-cases.
Use the optimization formulation from @PRD_v2.md#8-optimization-engine-specifications.
```

**For debugging optimization:**
```
The optimizer is returning infeasible. Check constraints against @PRD_v2.md#8-1-mathematical-formulation.
Verify departure SoC constraint is correctly implemented.
```

**For adding a new API endpoint:**
```
Add the endpoint specified in @PRD_v2.md#7-1-rest-api-endpoints.
Follow the existing patterns in src/api/main.py.
```

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-12-04 | Claude + Joris | Initial MVP PRD |
| 2.0 | 2025-12-12 | Claude + Joris | Reconciled with dev plan; added Gurobi config, building load, inter-depot handoffs, return time trigger, charger-vehicle access, vehicle max_charge_kw from OCPP |
| 2.1 | 2025-12-12 | Claude | Fixed inconsistencies: corrected OCPP WebSocket URL to use `{ocpp_id}`, clarified TOU pricing hours, fixed battery dynamics formula (removed incorrect efficiency division), updated all document references from PRD.md to PRD_v2.md |
| 2.2 | 2025-12-13 | Claude | Security & logic hardening: added vehicle count constraint to MILP, fixed grid power balance to use P_batt_effective, clarified incoming vehicle SoC initialization, added comprehensive security requirements (input validation, rate limiting, SQL injection prevention), added database CHECK constraints, added infeasibility handling specification, added data freshness requirements |
| 2.3 | 2025-12-13 | Claude | Reliability improvements: Added HiGHS fallback solver for graceful degradation when Gurobi fails (license error, connection issues), added solver_used field to OptimizationResult for monitoring, added Gurobi license failure test to integration tests |

---

*End of Product Requirements Document*
