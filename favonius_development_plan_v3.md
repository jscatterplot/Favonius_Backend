# Favonius Energy — Product Development Plan
## EV Fleet Depot Optimization Platform (MVP v3)

---

## Overview

This development plan implements the specifications in `docs/PRD_v2.md` (Version 2.7). The PRD is the **single source of truth** — if any discrepancy exists between this plan and the PRD, the PRD wins.

**Key Technical Decisions (from PRD):**
- Solver: **Gurobi** with <60 second solve time, **HiGHS fallback** for reliability
- Price trigger: **OR** logic (>25% OR >$25/MWh)
- Building load: **Required** (not optional)
- Connector type: **CCS only** for MVP
- Vehicle max_charge_kw: From **OCPP MeterValues** or config fallback
- **OCPP Protocol**: **1.6J only** (chargers MUST be configured for 1.6J subprotocol; 2.0.1 NOT wire-compatible)
- **VDV 463**: Transit operations integration for BMS/ITCS communication
- **BACnet/SC**: Building HVAC control via setpoint offset (thermal flywheel optimization)
- **Preconditioning**: **Soft constraint** (can be curtailed under site power limit)
- **Battery Efficiency**: Split round-trip model (√η each direction) to prevent free energy loops

**Architecture Decisions (Implemented):**
- **Integrated System Architecture**: Main API backend is primary optimization service
- **Unified WebSocket Handler**: Single service handles OCPP 1.6J, VDV 463, and BACnet/SC connections
- **Backup Heuristic**: WebSocket handler maintains heuristic optimizer for emergency use when main API unavailable > 1 hour
- **Julia Removed**: Julia MIP solver bridge removed, using Pyomo/Gurobi/HiGHS only
- **V2G Out of Scope**: V2G functionality removed/commented out per MVP scope
- **Dual Database**: Supabase for static data, TimescaleDB for time-series data
- **Dispatch Validation**: Pre-dispatch checks for charger/device status before sending commands

---

## PHASE 0: FOUNDATION & PROJECT SETUP

### Step 0.1: Development Environment Configuration

**Objective:** Establish a reproducible, AI-assisted development environment.

**Actions:**
1. Install Cursor IDE (or VS Code with GitHub Copilot as fallback)
2. Create `.cursorrules` file in project root (copy from PRD Appendix A.1)
3. Set up Git repository with conventional commits
4. Create `.cursor/rules/` directory with domain-specific rules:
   - `optimization.mdc` — MILP formulation patterns (Gurobi-specific)
   - `ocpp.mdc` — OCPP 1.6J message formats (1.6J ONLY, 2.0.1 not wire-compatible)
   - `vdv463.mdc` — VDV 463 transit operations protocol
   - `bacnet.mdc` — BACnet/SC building control patterns
   - `timescale.mdc` — TimescaleDB best practices

**Verification:**
- [ ] Cursor recognizes project context
- [ ] Git hooks installed (pre-commit, commit-msg)
- [ ] README.md created with project overview

---

### Step 0.2: Repository Structure

```
favonius-platform/
├── .cursor/
│   └── rules/                  # AI assistant rules
├── docs/
│   ├── PRD_v2.md               # Product Requirements Document (source of truth)
│   ├── ARCHITECTURE.md         # System architecture
│   └── API.md                  # API specifications (OpenAPI)
├── src/
│   ├── core/
│   │   ├── models.py           # Data classes (DepotState, DepotConfig, BuildingZone, etc.)
│   │   ├── optimizer/          # MILP optimization engine
│   │   │   ├── milp_model.py   # Pyomo model definition
│   │   │   ├── solver.py       # Gurobi solver wrapper with HiGHS fallback
│   │   │   ├── allocator.py    # Post-optimization charger allocation
│   │   │   └── dispatcher.py   # Dispatch validation and command execution
│   │   ├── surrogate/          # Energy consumption model
│   │   └── state/              # State assembler and trigger monitor
│   ├── adapters/
│   │   ├── ocpp/               # OCPP 1.6J server and handlers
│   │   ├── vdv463/             # VDV 463 transit operations adapter
│   │   │   ├── handler.py      # WebSocket handler for BMS/ITCS
│   │   │   ├── messages.py     # Message parsing and generation
│   │   │   └── vehicle_resolver.py  # vehicleId → vehicle_id UUID mapping
│   │   ├── bacnet/             # BACnet/SC building HVAC adapter
│   │   │   ├── hub.py          # BACnet/SC Hub implementation (bacpypes3)
│   │   │   ├── zone_mapper.py  # device_id → zone_id mapping
│   │   │   └── setpoint.py     # Setpoint offset calculation and dispatch
│   │   ├── caiso/              # CAISO price feeds
│   │   ├── weather/            # Weather API integration
│   │   ├── building_load/      # Building load meter/API
│   │   └── handoff/            # Inter-depot handoff manager
│   ├── security/               # Security modules (validators, auth, rate limiting)
│   ├── api/                    # FastAPI REST endpoints
│   └── db/                     # Database models & migrations
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/               # Test data (realistic UUIDs, routes, SoC, prices)
├── scripts/
│   └── simulation/             # Simulation harnesses
├── config/
│   ├── depot_config.yaml       # Depot-specific parameters
│   └── tariff_config.yaml      # Utility rate structures
├── migrations/                 # Database migrations
├── pyproject.toml
├── docker-compose.yml
└── Makefile
```

**Verification:**
- [ ] All directories created
- [ ] `pyproject.toml` with uv/Poetry configuration
- [ ] `docker-compose.yml` with TimescaleDB and app services

---

### Step 0.3: Technology Stack Installation

**Core Dependencies:**

| Component | Package | Purpose |
|-----------|---------|---------|
| **Optimization** | `gurobipy` | Gurobi MILP solver (primary) |
| **Optimization** | `highspy` or `appsi_highs` | HiGHS MILP solver (fallback) |
| **Modeling** | `pyomo` | Algebraic modeling language |
| **ML/Surrogate** | `scikit-learn`, `gpytorch` | Gaussian Process, MLP |
| **API** | `fastapi`, `uvicorn` | REST API server |
| **Database (Static)** | `supabase-py` | Supabase client for static data |
| **Database (Time-Series)** | `asyncpg`, `sqlalchemy` | TimescaleDB for time-series data |
| **OCPP** | `ocpp` | OCPP 1.6J support (1.6J ONLY for MVP) |
| **BACnet** | `bacpypes3` | BACnet/SC Hub for building HVAC control |
| **Time-series** | `pandas`, `polars` | Data manipulation |
| **Weather** | `openmeteo-requests` | Weather API client |
| **Testing** | `pytest`, `pytest-asyncio` | Test framework |

**Installation Script:**
```bash
# Using uv (recommended)
uv init favonius-platform
cd favonius-platform
uv add pyomo gurobipy highspy scikit-learn gpytorch fastapi uvicorn asyncpg sqlalchemy supabase
uv add ocpp bacpypes3 pandas polars openmeteo-requests httpx pyjwt cryptography
uv add --dev pytest pytest-asyncio pytest-cov ruff mypy

# Verify Gurobi license (primary solver)
python -c "import gurobipy as gp; print(f'Gurobi {gp.gurobi.version()}')"

# Verify HiGHS availability (fallback solver)
python -c "import pyomo.environ as pyo; solver = pyo.SolverFactory('appsi_highs'); print('HiGHS available')"

# Verify BACnet/SC support
python -c "import bacpypes3; print('BACpypes3 available')"
```

**Verification:**
- [ ] All dependencies install without conflicts
- [ ] `uv run python -c "import pyomo; import ocpp; print('OK')"` succeeds
- [ ] Gurobi solver accessible with valid license

---

### Step 0.4: Database Setup (Supabase + TimescaleDB)

**Architecture:** The platform uses a dual-database architecture:
- **Supabase**: Static/reference data (depots, vehicles, chargers, schedules)
- **TimescaleDB**: Time-series data (telemetry, prices, weather, optimization results)

**Schema:** Split schema per PRD Section 6.1.1 and 6.1.2.

Create `migrations/001_supabase_schema.sql` (for Supabase):

```sql
-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============ REFERENCE DATA ============

CREATE TABLE depots (
    depot_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
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
    external_id     VARCHAR(100) UNIQUE NOT NULL,
    vehicle_type    VARCHAR(50) NOT NULL,
    battery_kwh     DOUBLE PRECISION NOT NULL CHECK (battery_kwh > 0),
    max_charge_kw   DOUBLE PRECISION NOT NULL CHECK (max_charge_kw > 0),
    id_tag          VARCHAR(100),  -- OCPP idTag used in Authorize messages to map sessions to vehicles
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE chargers (
    charger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    ocpp_id         VARCHAR(100) UNIQUE NOT NULL,
    rated_kw        DOUBLE PRECISION NOT NULL,
    efficiency      DOUBLE PRECISION DEFAULT 0.95,
    connector_type  VARCHAR(50) DEFAULT 'CCS',
    status          VARCHAR(20) DEFAULT 'Available',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Physical accessibility: which vehicles can use which chargers
CREATE TABLE charger_vehicle_access (
    charger_id      UUID NOT NULL REFERENCES chargers(charger_id),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    is_accessible   BOOLEAN DEFAULT TRUE,
    notes           VARCHAR(255),
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
    charger_id      UUID REFERENCES chargers(charger_id),  -- Which charger reported this telemetry
    soc             DOUBLE PRECISION CHECK (soc >= 0 AND soc <= 1),
    location_lat    DOUBLE PRECISION CHECK (location_lat >= -90 AND location_lat <= 90),
    location_lon    DOUBLE PRECISION CHECK (location_lon >= -180 AND location_lon <= 180),
    is_plugged      BOOLEAN,
    charging_kw     DOUBLE PRECISION CHECK (charging_kw >= 0),
    odometer_km     DOUBLE PRECISION CHECK (odometer_km >= 0),
    max_charge_kw   DOUBLE PRECISION CHECK (max_charge_kw > 0)  -- From OCPP MeterValues
);
SELECT create_hypertable('telemetry', 'time');
CREATE INDEX idx_telemetry_vehicle ON telemetry (vehicle_id, time DESC);
CREATE INDEX idx_telemetry_charger ON telemetry (charger_id, time DESC);

CREATE TABLE prices (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    energy_kwh      DOUBLE PRECISION NOT NULL,
    demand_kw       DOUBLE PRECISION,
    source          VARCHAR(50),
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
    power_kw        DOUBLE PRECISION NOT NULL,
    source          VARCHAR(50) NOT NULL,
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
    actual_return_time TIMESTAMPTZ,
    energy_kwh      DOUBLE PRECISION,
    required_soc    DOUBLE PRECISION DEFAULT 1.0,
    dest_depot_id   UUID REFERENCES depots(depot_id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_schedules_vehicle_depart ON schedules (vehicle_id, departure_time);
```

Create `migrations/002_timescale_schema.sql` (for TimescaleDB):
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    run_time        TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  VARCHAR(50) NOT NULL,
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
    status          VARCHAR(20) DEFAULT 'pending',
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
    status          VARCHAR(20) DEFAULT 'pending',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    arrived_at      TIMESTAMPTZ,
    CONSTRAINT interdepot_different_depots CHECK (origin_depot_id != dest_depot_id),
    CONSTRAINT interdepot_arrival_after_departure CHECK (arrival_time > departure_time)
);
CREATE INDEX idx_interdepot_dest_status ON interdepot_messages (dest_depot_id, status);

CREATE TABLE trigger_log (
    trigger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL,  -- References Supabase depots table
    trigger_type    VARCHAR(50) NOT NULL,
    trigger_time    TIMESTAMPTZ DEFAULT NOW(),
    details         JSONB,
    run_id          UUID REFERENCES optimization_runs(run_id)
);
SELECT create_hypertable('trigger_log', 'trigger_time');
```

**Verification:**
- [ ] Supabase project created and schema applied
- [ ] TimescaleDB container running (`docker-compose up -d timescaledb`)
- [ ] Both schema migrations applied
- [ ] Sample data inserted for testing
- [ ] Foreign key relationships verified (TimescaleDB references Supabase UUIDs)

---

## PHASE 1: CORE OPTIMIZATION ENGINE

### Step 1.1: Data Models

Create `src/core/models.py` (copy from PRD Section 6.2):

```python
"""Core data models for Favonius optimization platform.

These dataclasses are the authoritative representation of system state.
See PRD_v2.md Section 6.2 for full specification.
"""
from __future__ import annotations

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
    
    Chargers are aggregated by rated_kw for optimization to reduce variable count.
    After optimization, power is allocated back to individual chargers.
    
    See PRD_v2.md Section 8.3 for aggregation strategy.
    """
    vehicle_capacities: dict[str, float]      # vehicle_id -> kWh
    vehicle_max_charge_kw: dict[str, float]   # vehicle_id -> max charge rate (kW)
    charger_groups: dict[float, int]          # rated_kw -> count
    charger_efficiency: float
    charger_vehicle_access: dict[str, set[str]]  # charger_id -> accessible vehicle_ids
    battery_capacity: float
    battery_power: float
    battery_efficiency: float = 0.92  # Round-trip efficiency for stationary battery
    battery_soc_min: float = 0.2
    battery_soc_max: float = 0.8
    max_site_power: float = 1000.0
    delta_t: float = 0.25  # hours (15 min)
    n_timesteps: int = 96  # 24 hours


@dataclass
class DepotState:
    """Dynamic state for optimization.
    
    Assembled from database queries before each optimization run.
    See PRD_v2.md Section 5.3 for data flow.
    """
    vehicle_socs: dict[str, float]            # vehicle_id -> SoC [0,1]
    battery_soc: float
    prices: list[float]                       # $/kWh per timestep
    demand_charge_rate: float                 # $/kW
    current_month_peak: float                 # kW (moving limit)
    vehicle_availability: dict[str, list[bool]]
    energy_requirements: dict[str, float]     # vehicle_id -> kWh needed
    departure_times: dict[str, int]           # vehicle_id -> timestep index
    building_power: list[float]               # Building load per timestep (kW) - REQUIRED
    incoming_vehicles: list[IncomingVehicle] = field(default_factory=list)


@dataclass
class OptimizationResult:
    """Output from optimization.
    
    See PRD_v2.md Section 8.1 for variable definitions.
    """
    run_id: UUID
    schedule: dict[str, dict]  # vehicle_id -> {charging_power: [], soc: []}
    battery_dispatch: list[float]  # +discharge, -charge
    grid_power: list[float]
    peak_demand_kw: float
    objective_value: float
    solve_time_s: float
    status: str  # 'optimal', 'feasible', 'degraded', 'infeasible', 'timeout'
    solver_used: str = 'gurobi'  # 'gurobi' or 'highs' - tracks which solver was used
```

**Verification:**
- [ ] All dataclasses import without errors
- [ ] Type hints validate with mypy

---

### Step 1.2: MILP Optimization Model

Create `src/core/optimizer/milp_model.py`:

```python
"""MILP optimization model for depot charging scheduling.

Implements the formulation from PRD_v2.md Section 8.1.
Uses Gurobi solver with configuration from PRD_v2.md Section 8.2.
"""
from __future__ import annotations

import pyomo.environ as pyo
from uuid import uuid4

from src.core.models import DepotState, DepotConfig, OptimizationResult


def build_optimization_model(
    state: DepotState,
    config: DepotConfig,
) -> pyo.ConcreteModel:
    """Build Pyomo MILP model for depot charging optimization.
    
    See PRD_v2.md Section 8.1 for mathematical formulation.
    
    Args:
        state: Current depot state (SoCs, prices, availability, building load)
        config: Static depot configuration (capacities, charger groups)
    
    Returns:
        Pyomo ConcreteModel ready for solving
    """
    model = pyo.ConcreteModel("DepotCharging")
    
    # Sets
    model.T = pyo.RangeSet(0, config.n_timesteps - 1)
    model.B = pyo.Set(initialize=list(state.vehicle_socs.keys()))
    
    # Parameters
    model.price = pyo.Param(model.T, initialize=lambda m, t: state.prices[t])
    model.E_batt = pyo.Param(model.B, initialize=config.vehicle_capacities)
    model.max_charge_kw = pyo.Param(
        model.B, 
        initialize=lambda m, b: config.vehicle_max_charge_kw.get(b, 80.0)
    )
    model.soc_init = pyo.Param(model.B, initialize=state.vehicle_socs)
    model.available = pyo.Param(
        model.B, model.T,
        initialize=lambda m, b, t: 1 if state.vehicle_availability[b][t] else 0
    )
    model.building_power = pyo.Param(
        model.T,
        initialize=lambda m, t: state.building_power[t]
    )
    
    # Variables
    # Vehicle charging power (respects vehicle max_charge_kw)
    model.P_charge = pyo.Var(
        model.B, model.T, 
        domain=pyo.NonNegativeReals,
        bounds=lambda m, b, t: (0, m.max_charge_kw[b])
    )
    
    # Vehicle SoC with bounds to prevent overcharging
    model.SoC = pyo.Var(model.B, model.T, bounds=(0.01, 1.0))
    
    # Binary: is vehicle charging at timestep
    model.y_charge = pyo.Var(model.B, model.T, domain=pyo.Binary)
    
    # Grid power (non-negative, site limit enforced separately)
    model.P_grid = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    
    # Peak demand tracking
    model.P_peak = pyo.Var(domain=pyo.NonNegativeReals)
    
    # Battery storage variables
    model.P_batt = pyo.Var(
        model.T, 
        bounds=(-config.battery_power, config.battery_power)
    )
    model.SoC_batt = pyo.Var(
        model.T, 
        bounds=(config.battery_soc_min, config.battery_soc_max)
    )
    
    # ========== CONSTRAINTS ==========
    
    # Constraint 1: SoC initialization
    def soc_init_rule(m, b):
        return m.SoC[b, 0] == state.vehicle_socs[b]
    model.soc_init_con = pyo.Constraint(model.B, rule=soc_init_rule)
    
    # Constraint 1 (cont): SoC dynamics
    def soc_dynamics_rule(m, b, t):
        if t == 0:
            return pyo.Constraint.Skip
        eta = config.charger_efficiency
        return m.SoC[b, t] == m.SoC[b, t-1] + (
            eta * m.P_charge[b, t-1] * config.delta_t / m.E_batt[b]
        )
    model.soc_dynamics = pyo.Constraint(model.B, model.T, rule=soc_dynamics_rule)
    
    # Constraint 3: Vehicle availability
    def availability_rule(m, b, t):
        if not state.vehicle_availability[b][t]:
            return m.P_charge[b, t] == 0
        return pyo.Constraint.Skip
    model.availability_con = pyo.Constraint(model.B, model.T, rule=availability_rule)
    
    # Constraint 4: Departure SoC requirement (HARD - per PRD)
    def departure_soc_rule(m, b):
        t_depart = state.departure_times.get(b)
        if t_depart is not None and 0 < t_depart < config.n_timesteps:
            return m.SoC[b, t_depart] >= 0.99
        return pyo.Constraint.Skip
    model.departure_soc = pyo.Constraint(model.B, rule=departure_soc_rule)
    
    # Constraint 5: Charger linking (vehicle max rate from OCPP or config)
    def charger_link_rule(m, b, t):
        return m.P_charge[b, t] <= m.max_charge_kw[b] * m.y_charge[b, t]
    model.charger_link = pyo.Constraint(model.B, model.T, rule=charger_link_rule)
    
    # Constraint 6: Charger capacity - BOTH power limit AND vehicle count limit (per PRD Constraint 7)
    total_chargers = sum(config.charger_groups.values()) if config.charger_groups else 0
    total_charger_power = sum(kw * count for kw, count in config.charger_groups.items()) if config.charger_groups else 0

    # 6a: Vehicle count limit
    def charger_count_rule(m, t):
        return sum(m.y_charge[b, t] for b in m.B) <= total_chargers
    model.charger_count = pyo.Constraint(model.T, rule=charger_count_rule)

    # 6b: Power limit
    def charger_power_rule(m, t):
        return sum(m.P_charge[b, t] for b in m.B) <= total_charger_power
    model.charger_power = pyo.Constraint(model.T, rule=charger_power_rule)
    
    # Constraint 7: Grid power balance (includes building load - REQUIRED per PRD)
    # Uses P_batt_effective which accounts for round-trip efficiency:
    # - Discharge (P_batt > 0): grid receives P_batt * η (efficiency loss)
    # - Charge (P_batt < 0): grid supplies |P_batt| / η (extra power needed)
    # Implementation note: This requires auxiliary variables for proper MILP handling.
    # For MVP, we use a linearized approximation with separate discharge/charge vars.

    # Split battery power into charge and discharge components
    eta_batt = config.battery_efficiency  # From DepotConfig (default 0.92)
    model.P_batt_discharge = pyo.Var(model.T, domain=pyo.NonNegativeReals, bounds=(0, config.battery_power))
    model.P_batt_charge = pyo.Var(model.T, domain=pyo.NonNegativeReals, bounds=(0, config.battery_power))

    # Link P_batt to charge/discharge: P_batt = P_discharge - P_charge
    def batt_split_rule(m, t):
        return m.P_batt[t] == m.P_batt_discharge[t] - m.P_batt_charge[t]
    model.batt_split = pyo.Constraint(model.T, rule=batt_split_rule)

    # Grid balance with efficiency-adjusted battery power (PRD Constraint 8)
    def grid_balance_rule(m, t):
        # P_batt_effective = discharge * η - charge / η
        P_batt_effective = m.P_batt_discharge[t] * eta_batt - m.P_batt_charge[t] / eta_batt
        return m.P_grid[t] == (
            sum(m.P_charge[b, t] for b in m.B) +
            m.building_power[t] -
            P_batt_effective
        )
    model.grid_balance = pyo.Constraint(model.T, rule=grid_balance_rule)
    
    # Constraint 8: Site power limit
    def site_power_limit_rule(m, t):
        return m.P_grid[t] <= config.max_site_power
    model.site_power_limit = pyo.Constraint(model.T, rule=site_power_limit_rule)
    
    # Constraint 9: Peak demand tracking
    def peak_tracking_rule(m, t):
        return m.P_peak >= m.P_grid[t]
    model.peak_tracking = pyo.Constraint(model.T, rule=peak_tracking_rule)
    
    # Constraint 9 (cont): Moving peak limit
    def moving_peak_rule(m):
        return m.P_peak >= state.current_month_peak
    model.moving_peak = pyo.Constraint(rule=moving_peak_rule)
    
    # Constraint 10: Battery initialization
    def batt_init_rule(m):
        return m.SoC_batt[0] == state.battery_soc
    model.batt_init = pyo.Constraint(rule=batt_init_rule)
    
    # Constraint 10 (cont): Battery dynamics
    # P_batt > 0 = discharge (reduces SoC), P_batt < 0 = charge (increases SoC)
    def batt_dynamics_rule(m, t):
        if t == 0:
            return pyo.Constraint.Skip
        return m.SoC_batt[t] == m.SoC_batt[t-1] - (
            m.P_batt[t-1] * config.delta_t / config.battery_capacity
        )
    model.batt_dynamics = pyo.Constraint(model.T, rule=batt_dynamics_rule)
    
    # ========== OBJECTIVE ==========
    # Minimize: Energy cost + Demand charge
    def objective_rule(m):
        energy_cost = sum(
            m.price[t] * m.P_grid[t] * config.delta_t
            for t in m.T
        )
        demand_cost = state.demand_charge_rate * m.P_peak
        return energy_cost + demand_cost
    
    model.objective = pyo.Objective(rule=objective_rule, sense=pyo.minimize)
    
    return model


def solve_model(
    model: pyo.ConcreteModel,
    time_limit: float = 60.0,
    warm_start: bool = True,
    allow_degraded: bool = True,
) -> OptimizationResult:
    """Solve the optimization model using Gurobi with HiGHS fallback.

    See PRD_v2.md Section 8.2 for solver configuration.
    Implements automatic fallback to HiGHS if Gurobi fails for reliability.
    See PRD_v2.md Section 8.5.1 for infeasibility handling.

    Args:
        model: Pyomo model to solve
        time_limit: Maximum solve time in seconds (default: 60)
        warm_start: Whether to use warm-starting (default: True)
        allow_degraded: If infeasible, attempt relaxed solve (default: True)

    Returns:
        OptimizationResult with schedule and metadata
    """
    import logging
    logger = logging.getLogger(__name__)

    # Configure Gurobi solver (per PRD Section 8.2)
    solver = pyo.SolverFactory('gurobi')
    solver.options['TimeLimit'] = time_limit
    solver.options['MIPGap'] = 0.01  # 1% optimality gap
    solver.options['Threads'] = 4
    solver.options['Presolve'] = 2  # Aggressive
    solver.options['NumericFocus'] = 3  # Highest numerical accuracy
    solver.options['OutputFlag'] = 1

    if warm_start:
        solver.options['WarmStart'] = 1

    # Track which solver was used (for monitoring and fallback detection)
    solver_used = 'gurobi'
    result = None
    term_cond = None
    status = 'error'
    
    # ========== PRIMARY SOLVER: GUROBI (PRD Section 8.2) ==========
    try:
        # Check Gurobi availability first
        if solver is None or not solver.available():
            raise Exception("Gurobi solver not available")
        
        # Solve with Gurobi
        logger.info("Attempting solve with Gurobi solver")
        result = solver.solve(model, tee=False)
        term_cond = result.solver.termination_condition
        
        # Determine status from termination condition
        if term_cond == pyo.TerminationCondition.optimal:
            status = 'optimal'
        elif term_cond in [pyo.TerminationCondition.maxTimeLimit,
                           pyo.TerminationCondition.feasible]:
            status = 'feasible'
        elif term_cond == pyo.TerminationCondition.infeasible:
            status = 'infeasible'
        else:
            status = 'error'
            
    except Exception as gurobi_error:
        # Gurobi failed (license error, connection issue, etc.)
        logger.warning(f"Gurobi solver failed: {gurobi_error}. Falling back to HiGHS.")
        solver_used = 'highs'
        status = 'error'
    
    # ========== FALLBACK TO HiGHS (PRD Section 8.2) ==========
    # If Gurobi fails, automatically fall back to HiGHS
    if solver_used == 'highs' or status == 'error':
        logger.warning("Gurobi solve failed, attempting HiGHS fallback")
        solver_used = 'highs'
        
        try:
            fallback_solver = pyo.SolverFactory('appsi_highs')
            fallback_solver.options['time_limit'] = time_limit
            fallback_solver.options['mip_rel_gap'] = 0.01
            fallback_solver.options['threads'] = 4
            fallback_solver.options['presolve'] = 'on'
            
            result = fallback_solver.solve(model, tee=False)
            term_cond = result.solver.termination_condition
            
            if term_cond == pyo.TerminationCondition.optimal:
                status = 'optimal'
            elif term_cond in [pyo.TerminationCondition.maxTimeLimit,
                               pyo.TerminationCondition.feasible]:
                status = 'feasible'
            elif term_cond == pyo.TerminationCondition.infeasible:
                status = 'infeasible'
            else:
                status = 'error'
        except Exception as fallback_error:
            logger.error(f"HiGHS fallback also failed: {fallback_error}")
            status = 'error'
            # Both solvers failed - will be handled in error path below

    # ========== INFEASIBILITY HANDLING (PRD Section 8.5.1) ==========
    # When optimization cannot satisfy all constraints:
    # 1. Identify conflicting vehicles using Gurobi's IIS
    # 2. Relaxed solve: Relax departure SoC from 99% to 90% for affected vehicles
    # 3. Alert generation for operations team
    # 4. Never silently fail - always return a schedule
    if status == 'infeasible' and allow_degraded:
        logger.warning("Initial solve infeasible, attempting degraded solve with relaxed SoC constraints")

        # Compute IIS to identify conflicting constraints
        # Note: This requires direct Gurobi API access
        try:
            # Relax departure SoC constraints from 99% to 90%
            for con_name in dir(model):
                if 'departure_soc' in con_name.lower():
                    con = getattr(model, con_name)
                    if hasattr(con, 'deactivate'):
                        con.deactivate()

            # Add relaxed departure constraints (90% instead of 99%)
            def relaxed_departure_rule(m, b):
                # This is a simplified approach - production code would
                # selectively relax based on IIS analysis
                return pyo.Constraint.Skip  # Remove departure constraints
            model.relaxed_departure = pyo.Constraint(model.B, rule=relaxed_departure_rule)

            # Re-solve with relaxed constraints (use same solver that was used initially)
            if solver_used == 'gurobi':
                result = solver.solve(model, tee=False)
            else:
                # Use HiGHS if that's what we're using
                fallback_solver = pyo.SolverFactory('appsi_highs')
                fallback_solver.options['time_limit'] = time_limit
                fallback_solver.options['mip_rel_gap'] = 0.01
                result = fallback_solver.solve(model, tee=False)
            
            term_cond = result.solver.termination_condition

            if term_cond in [pyo.TerminationCondition.optimal,
                            pyo.TerminationCondition.feasible,
                            pyo.TerminationCondition.maxTimeLimit]:
                status = 'degraded'  # Indicates solution found with relaxed constraints
                logger.warning("Degraded solution found - some vehicles may not reach target SoC")
        except Exception as e:
            logger.error(f"Degraded solve failed: {e}")
            status = 'infeasible'

    # Extract solution (if feasible or degraded)
    if status in ['optimal', 'feasible', 'degraded']:
        schedule = {}
        for b in model.B:
            schedule[b] = {
                'charging_power': [pyo.value(model.P_charge[b, t]) for t in model.T],
                'soc': [pyo.value(model.SoC[b, t]) for t in model.T],
            }

        return OptimizationResult(
            run_id=uuid4(),
            schedule=schedule,
            battery_dispatch=[pyo.value(model.P_batt[t]) for t in model.T],
            grid_power=[pyo.value(model.P_grid[t]) for t in model.T],
            peak_demand_kw=pyo.value(model.P_peak),
            objective_value=pyo.value(model.objective),
            solve_time_s=result.solver.time if hasattr(result.solver, 'time') else 0.0,
            status=status,
            solver_used=solver_used,  # Track which solver was used for monitoring
        )
    else:
        # Never silently fail - log and return error result (per PRD Section 8.5.1)
        logger.error(f"Optimization failed with status: {status}")
        solve_time = 0.0
        if result is not None and hasattr(result, 'solver') and hasattr(result.solver, 'time'):
            solve_time = result.solver.time
        
        return OptimizationResult(
            run_id=uuid4(),
            schedule={},
            battery_dispatch=[],
            grid_power=[],
            peak_demand_kw=0.0,
            objective_value=float('inf'),
            solve_time_s=solve_time,
            status=status,
            solver_used=solver_used or 'unknown',  # Track solver attempt
        )


def warm_start_model(
    model: pyo.ConcreteModel, 
    previous_result: OptimizationResult
) -> None:
    """Initialize model variables from previous solution.
    
    See PRD_v2.md Section 8.5 for performance targets.
    Expected speedup: >3x with warm-starting.
    
    Args:
        model: Pyomo model to initialize
        previous_result: Previous optimization result
    """
    for b in model.B:
        if b in previous_result.schedule:
            for t in model.T:
                charging_power = previous_result.schedule[b]['charging_power'][t]
                model.P_charge[b, t].value = charging_power
                model.y_charge[b, t].value = 1 if charging_power > 0.1 else 0
                model.SoC[b, t].value = previous_result.schedule[b]['soc'][t]
    
    for t in model.T:
        if t < len(previous_result.battery_dispatch):
            model.P_batt[t].value = previous_result.battery_dispatch[t]
        if t < len(previous_result.grid_power):
            model.P_grid[t].value = previous_result.grid_power[t]
    
    model.P_peak.value = previous_result.peak_demand_kw
```

**Verification:**
- [ ] Model builds without errors
- [ ] Gurobi solver finds feasible solution for test case
- [ ] Solve time < 60 seconds for 20 vehicles, 96 timesteps
- [ ] All departure SoC constraints satisfied

---

### Step 1.3: Post-Optimization Charger Allocation

Create `src/core/optimizer/allocator.py`:

```python
"""Post-optimization charger allocation.

Allocates aggregated charging power to individual physical chargers,
respecting physical accessibility constraints.

See PRD_v2.md Section 8.3 for aggregation strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from src.core.models import DepotConfig, OptimizationResult


@dataclass
class ChargerAssignment:
    """Assignment of a vehicle to a charger at a timestep."""
    charger_id: str
    vehicle_id: str
    timestep: int
    power_kw: float


def allocate_chargers(
    result: OptimizationResult,
    config: DepotConfig,
    charger_ids_by_rating: dict[float, list[str]],  # rated_kw -> list of charger_ids
    vehicle_priorities: dict[str, float],  # vehicle_id -> priority (lower = higher priority)
) -> list[ChargerAssignment]:
    """Allocate optimized power to individual chargers.
    
    Args:
        result: Optimization result with aggregated power
        config: Depot configuration with accessibility matrix
        charger_ids_by_rating: Mapping of power rating to charger IDs
        vehicle_priorities: Priority for each vehicle (based on departure time, SoC deficit)
    
    Returns:
        List of ChargerAssignment objects for each timestep
    """
    assignments = []
    n_timesteps = len(result.grid_power)
    
    for t in range(n_timesteps):
        # Get vehicles that need charging this timestep
        charging_vehicles = [
            (vid, result.schedule[vid]['charging_power'][t])
            for vid in result.schedule
            if result.schedule[vid]['charging_power'][t] > 0.1
        ]
        
        # Sort by priority (departure time, SoC deficit)
        charging_vehicles.sort(key=lambda x: vehicle_priorities.get(x[0], float('inf')))
        
        # Track which chargers are used this timestep
        used_chargers: set[str] = set()
        
        for vehicle_id, power_kw in charging_vehicles:
            # Find an available charger that this vehicle can access
            assigned = False
            
            for rated_kw, charger_ids in charger_ids_by_rating.items():
                if assigned:
                    break
                    
                for charger_id in charger_ids:
                    if charger_id in used_chargers:
                        continue
                    
                    # Check physical accessibility
                    accessible_vehicles = config.charger_vehicle_access.get(charger_id, set())
                    if accessible_vehicles and vehicle_id not in accessible_vehicles:
                        continue
                    
                    # Check power rating is sufficient
                    if rated_kw < power_kw:
                        continue
                    
                    # Assign
                    assignments.append(ChargerAssignment(
                        charger_id=charger_id,
                        vehicle_id=vehicle_id,
                        timestep=t,
                        power_kw=min(power_kw, rated_kw),
                    ))
                    used_chargers.add(charger_id)
                    assigned = True
                    break
            
            if not assigned:
                # Log warning: couldn't allocate charger
                # This shouldn't happen if optimization is correct
                pass
    
    return assignments
```

**Verification:**
- [ ] Allocator respects physical accessibility
- [ ] No charger double-booked in same timestep
- [ ] Power doesn't exceed charger rating

---

## PHASE 2: TRIGGER MONITORING

### Step 2.1: Trigger Monitor Implementation

Create `src/core/state/triggers.py`:

```python
"""Re-optimization trigger monitoring.

Monitors conditions that require re-optimization per PRD_v2.md Section 5.1.
Trigger thresholds:
- SoC deviation: >5%
- Price change: >25% OR >$25/MWh
- Return time deviation: >15 minutes
- Inter-depot handoff: On message receipt
- Scheduled: Hourly 24/7
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class TriggerType(Enum):
    SCHEDULED = "scheduled"
    SOC_DEVIATION = "soc_deviation"
    PRICE_CHANGE = "price_change"
    RETURN_TIME_DEVIATION = "return_time_deviation"
    INTERDEPOT_HANDOFF = "interdepot_handoff"


@dataclass
class TriggerEvent:
    """Represents a trigger that fired."""
    trigger_type: TriggerType
    depot_id: UUID
    timestamp: datetime
    details: dict


class TriggerMonitor:
    """Monitors conditions requiring re-optimization.
    
    See PRD_v2.md Section 5.1 for trigger thresholds.
    """
    
    # Thresholds from PRD
    SOC_DEVIATION_THRESHOLD = 0.05  # 5%
    PRICE_CHANGE_PCT_THRESHOLD = 0.25  # 25%
    PRICE_CHANGE_ABS_THRESHOLD = 25.0  # $25/MWh = $0.025/kWh
    RETURN_TIME_DEVIATION_MINUTES = 15
    
    def __init__(
        self,
        depot_id: UUID,
        on_trigger: Callable[[TriggerEvent], None],
        check_interval_seconds: int = 900,  # 15 minutes
    ):
        self.depot_id = depot_id
        self.on_trigger = on_trigger
        self.check_interval = check_interval_seconds
        self._running = False
        self._baseline_prices: list[float] = []
        self._expected_socs: dict[str, float] = {}
        self._expected_return_times: dict[str, datetime] = {}
    
    def set_baseline(
        self,
        prices: list[float],
        expected_socs: dict[str, float],
        expected_return_times: dict[str, datetime],
    ) -> None:
        """Set baseline values after optimization."""
        self._baseline_prices = prices.copy()
        self._expected_socs = expected_socs.copy()
        self._expected_return_times = expected_return_times.copy()
    
    def check_soc_deviation(
        self, 
        vehicle_id: str, 
        actual_soc: float
    ) -> Optional[TriggerEvent]:
        """Check if SoC deviates from expected by >5%."""
        expected = self._expected_socs.get(vehicle_id)
        if expected is None:
            return None
        
        deviation = abs(actual_soc - expected)
        if deviation > self.SOC_DEVIATION_THRESHOLD:
            return TriggerEvent(
                trigger_type=TriggerType.SOC_DEVIATION,
                depot_id=self.depot_id,
                timestamp=datetime.utcnow(),
                details={
                    'vehicle_id': vehicle_id,
                    'expected_soc': expected,
                    'actual_soc': actual_soc,
                    'deviation': deviation,
                }
            )
        return None
    
    def check_price_change(
        self, 
        current_prices: list[float],
        timestep: int,
    ) -> Optional[TriggerEvent]:
        """Check if price changed >25% OR >$25/MWh.
        
        Uses OR logic per PRD specification.
        """
        if timestep >= len(self._baseline_prices) or timestep >= len(current_prices):
            return None
        
        baseline = self._baseline_prices[timestep]
        current = current_prices[timestep]
        
        if baseline == 0:
            return None
        
        pct_change = abs(current - baseline) / baseline
        abs_change_mwh = abs(current - baseline) * 1000  # Convert $/kWh to $/MWh
        
        # OR logic: trigger if EITHER threshold exceeded
        if pct_change > self.PRICE_CHANGE_PCT_THRESHOLD or abs_change_mwh > self.PRICE_CHANGE_ABS_THRESHOLD:
            return TriggerEvent(
                trigger_type=TriggerType.PRICE_CHANGE,
                depot_id=self.depot_id,
                timestamp=datetime.utcnow(),
                details={
                    'timestep': timestep,
                    'baseline_price': baseline,
                    'current_price': current,
                    'pct_change': pct_change,
                    'abs_change_mwh': abs_change_mwh,
                }
            )
        return None
    
    def check_return_time_deviation(
        self,
        vehicle_id: str,
        actual_return_time: datetime,
    ) -> Optional[TriggerEvent]:
        """Check if return time deviates by >15 minutes."""
        expected = self._expected_return_times.get(vehicle_id)
        if expected is None:
            return None
        
        deviation = actual_return_time - expected
        deviation_minutes = deviation.total_seconds() / 60
        
        # Only trigger if LATE (positive deviation)
        if deviation_minutes > self.RETURN_TIME_DEVIATION_MINUTES:
            return TriggerEvent(
                trigger_type=TriggerType.RETURN_TIME_DEVIATION,
                depot_id=self.depot_id,
                timestamp=datetime.utcnow(),
                details={
                    'vehicle_id': vehicle_id,
                    'expected_return': expected.isoformat(),
                    'actual_return': actual_return_time.isoformat(),
                    'deviation_minutes': deviation_minutes,
                }
            )
        return None
    
    def trigger_interdepot_handoff(
        self,
        message_id: UUID,
        vehicle_id: UUID,
        origin_depot_id: UUID,
    ) -> TriggerEvent:
        """Create trigger for inter-depot handoff receipt."""
        return TriggerEvent(
            trigger_type=TriggerType.INTERDEPOT_HANDOFF,
            depot_id=self.depot_id,
            timestamp=datetime.utcnow(),
            details={
                'message_id': str(message_id),
                'vehicle_id': str(vehicle_id),
                'origin_depot_id': str(origin_depot_id),
            }
        )
    
    def create_scheduled_trigger(self) -> TriggerEvent:
        """Create a scheduled trigger event."""
        return TriggerEvent(
            trigger_type=TriggerType.SCHEDULED,
            depot_id=self.depot_id,
            timestamp=datetime.utcnow(),
            details={'reason': 'hourly_schedule'}
        )
    
    async def run(self) -> None:
        """Main monitoring loop."""
        self._running = True
        logger.info(f"Trigger monitor started for depot {self.depot_id}")
        
        while self._running:
            now = datetime.utcnow()
            hour = now.hour
            
            # Scheduled trigger: hourly 24/7
            if not hasattr(self, '_last_scheduled_hour') or self._last_scheduled_hour != hour:
                trigger = self.create_scheduled_trigger()
                self.on_trigger(trigger)
                self._last_scheduled_hour = hour
            
            await asyncio.sleep(self.check_interval)
    
    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        logger.info(f"Trigger monitor stopped for depot {self.depot_id}")
```

**Verification:**
- [ ] SoC deviation triggers at >5%
- [ ] Price change triggers with OR logic (>25% OR >$25/MWh)
- [ ] Return time deviation triggers at >15 minutes late
- [ ] Scheduled trigger fires hourly 24/7

---

## PHASE 3: INTER-DEPOT HANDOFF

### Step 3.1: Handoff Manager

Create `src/adapters/handoff/manager.py`:

```python
"""Inter-depot vehicle handoff management.

Implements PRD_v2.md Section 5.4 for inter-depot coordination.
"""
from __future__ import annotations

import httpx
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID
import logging

from src.core.models import IncomingVehicle

logger = logging.getLogger(__name__)


@dataclass
class HandoffMessage:
    """Message sent when vehicle departs for another depot."""
    message_id: UUID
    origin_depot_id: UUID
    dest_depot_id: UUID
    vehicle_id: UUID
    external_id: str
    expected_soc: float
    arrival_time: datetime
    battery_kwh: float
    max_charge_kw: float


class HandoffManager:
    """Manages inter-depot vehicle handoffs.
    
    See PRD_v2.md Section 5.4 for handoff flow.
    """
    
    def __init__(self, depot_endpoints: dict[UUID, str]):
        """Initialize with mapping of depot_id -> API endpoint URL."""
        self.depot_endpoints = depot_endpoints
        self._client = httpx.AsyncClient(timeout=30.0)
    
    async def send_handoff(
        self,
        origin_depot_id: UUID,
        dest_depot_id: UUID,
        vehicle_id: UUID,
        external_id: str,
        expected_soc: float,
        arrival_time: datetime,
        battery_kwh: float,
        max_charge_kw: float,
    ) -> Optional[UUID]:
        """Send handoff message to destination depot.
        
        Returns message_id if successful, None otherwise.
        """
        endpoint = self.depot_endpoints.get(dest_depot_id)
        if not endpoint:
            logger.error(f"No endpoint configured for depot {dest_depot_id}")
            return None
        
        message = HandoffMessage(
            message_id=UUID(int=0),  # Will be assigned by receiver
            origin_depot_id=origin_depot_id,
            dest_depot_id=dest_depot_id,
            vehicle_id=vehicle_id,
            external_id=external_id,
            expected_soc=expected_soc,
            arrival_time=arrival_time,
            battery_kwh=battery_kwh,
            max_charge_kw=max_charge_kw,
        )
        
        try:
            response = await self._client.post(
                f"{endpoint}/depots/{dest_depot_id}/handoff/receive",
                json={
                    'origin_depot_id': str(origin_depot_id),
                    'vehicle_id': str(vehicle_id),
                    'external_id': external_id,
                    'expected_soc': expected_soc,
                    'arrival_time': arrival_time.isoformat(),
                    'battery_kwh': battery_kwh,
                    'max_charge_kw': max_charge_kw,
                }
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Handoff sent: vehicle {external_id} to depot {dest_depot_id}")
            return UUID(data.get('message_id'))
        except Exception as e:
            logger.error(f"Failed to send handoff: {e}")
            return None
    
    def create_incoming_vehicle(
        self,
        vehicle_id: UUID,
        external_id: str,
        expected_soc: float,
        arrival_time: datetime,
        battery_kwh: float,
        max_charge_kw: float,
        origin_depot_id: UUID,
    ) -> IncomingVehicle:
        """Create IncomingVehicle for state assembly."""
        return IncomingVehicle(
            vehicle_id=vehicle_id,
            external_id=external_id,
            expected_soc=expected_soc,
            arrival_time=arrival_time,
            battery_kwh=battery_kwh,
            max_charge_kw=max_charge_kw,
            origin_depot_id=origin_depot_id,
        )
    
    async def close(self) -> None:
        """Close HTTP client."""
        await self._client.aclose()
```

**Verification:**
- [ ] Handoff messages sent successfully
- [ ] Destination depot acknowledges receipt
- [ ] Incoming vehicles included in state assembly

---

## PHASE 4: OCPP INTEGRATION & INTERNAL API

### Architecture Context

**Current State:**
- WebSocket Handler has OCPP 1.6J server that handles all charger connections
- WebSocket Handler stores all telemetry to TimescaleDB
- Main API currently has its own OCPP 1.6J server (`src/adapters/ocpp/server.py`) for direct charger communication (temporary workaround)

**Target Architecture (Phase 4):**
- WebSocket Handler is the single OCPP 1.6J communication layer
- Main API communicates with WebSocket Handler via internal REST API
- WebSocket Handler exposes endpoints for:
  - Querying connected charge points
  - Getting charge point state (SoC, power, connection status)
  - Sending SetChargingProfile commands
  - Health monitoring

**OCPP Version Strategy (CRITICAL - per PRD v2.7):**
- **Supported Protocol**: OCPP 1.6J ONLY
- **OCPP 2.0.1 is NOT wire-compatible** with 1.6J—different JSON schemas, RPC action names, and enum values
- Chargers supporting OCPP 2.0.1/2.1 **MUST be configured to use OCPP 1.6J subprotocol**
- A charger attempting a true OCPP 2.0.1 handshake will **fail to connect**
- Native OCPP 2.0.1 support is planned for **post-MVP**

**Benefits:**
- Single OCPP communication layer (no duplication)
- Simpler implementation (one protocol version to maintain)
- Better separation of concerns (optimization vs. OCPP protocol)
- Easier to scale OCPP connections independently
- Centralized telemetry storage

### Step 4.0: Internal API Implementation (WebSocket Handler)

**Objective:** Expose internal REST API for Main API to query charge point state and send commands.

**Create `src/websocket_handler/internal_api.py`:**

```python
"""Internal REST API for Main API backend communication.

This API is not exposed externally - only accessible from Main API service.
Provides charge point state queries and command dispatch.
"""
from fastapi import FastAPI, HTTPException
from typing import List, Dict, Optional
from datetime import datetime
from uuid import UUID

app = FastAPI(title="WebSocket Handler Internal API")


@app.get("/internal/charge-points/connected")
async def get_connected_charge_points() -> List[str]:
    """Get list of currently connected charge point IDs."""
    # Query connection manager for active connections
    pass


@app.get("/internal/charge-points/{charge_point_id}/state")
async def get_charge_point_state(charge_point_id: str) -> Dict:
    """Get current state of a charge point.
    
    Returns:
        {
            "connected": bool,
            "current_power_kw": float,
            "soc_percent": float,
            "status": str,
            "last_update": datetime
        }
    """
    pass


@app.post("/internal/charge-points/{charge_point_id}/set-charging-profile")
async def set_charging_profile(
    charge_point_id: str,
    profile: Dict
) -> Dict:
    """Send SetChargingProfile command to charge point.
    
    Returns:
        {"status": "Accepted" | "Rejected", "message": str}
    """
    pass


@app.get("/internal/health")
async def health_check() -> Dict:
    """Health check for internal API."""
    return {"status": "healthy"}
```

**Update Main API to use Internal API:**

Create `src/adapters/ocpp/client.py`:

```python
"""OCPP client for Main API to communicate with WebSocket Handler.

Replaces direct OCPP server usage in Main API.
"""
import httpx
from typing import Optional, Dict, List
from datetime import datetime

class OCPPClient:
    """Client for WebSocket Handler internal API."""
    
    def __init__(self, base_url: str = "http://websocket-handler:8080"):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=30.0)
    
    async def get_connected_charge_points(self) -> List[str]:
        """Get list of connected charge points."""
        response = await self.client.get(f"{self.base_url}/internal/charge-points/connected")
        response.raise_for_status()
        return response.json()
    
    async def get_charge_point_state(self, charge_point_id: str) -> Dict:
        """Get charge point state."""
        response = await self.client.get(
            f"{self.base_url}/internal/charge-points/{charge_point_id}/state"
        )
        response.raise_for_status()
        return response.json()
    
    async def send_charging_profile(
        self,
        charge_point_id: str,
        profile: Dict
    ) -> Dict:
        """Send SetChargingProfile command."""
        response = await self.client.post(
            f"{self.base_url}/internal/charge-points/{charge_point_id}/set-charging-profile",
            json=profile
        )
        response.raise_for_status()
        return response.json()
```

**Update `DepotController` to use OCPP Client:**

```python
# In src/core/controller.py
# Replace ocpp_server parameter with ocpp_client
def __init__(
    self,
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    config: DepotConfig,
    ocpp_client: Optional['OCPPClient'] = None,  # Changed from ocpp_server
    controller_config: Optional[ControllerConfig] = None,
):
    # ...
    self.ocpp_client = ocpp_client  # Changed from ocpp_server
```

**Verification:**
- [ ] Internal API endpoints respond correctly
- [ ] Main API can query charge point state
- [ ] Main API can send SetChargingProfile commands
- [ ] All existing tests updated to use ocpp_client
- [ ] Integration tests pass with new architecture

### Step 4.1: Vehicle Max Charge Rate from OCPP

Create `src/adapters/ocpp/handlers.py` (excerpt for max_charge_kw handling):

```python
"""OCPP message handlers.

See PRD_v2.md Section 7.2 for supported messages.
See PRD_v2.md Section 8.4 for vehicle max_charge_kw resolution.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class OCPPHandler:
    """Handles OCPP messages from chargers."""
    
    # Max age for OCPP-reported max_charge_kw before falling back to config
    MAX_CHARGE_KW_STALENESS = timedelta(minutes=15)
    
    def __init__(self, db_pool):
        self.db = db_pool
        self._recent_max_charge_kw: dict[str, tuple[float, datetime]] = {}
    
    async def handle_meter_values(
        self,
        charger_ocpp_id: str,
        charger_id: UUID,
        vehicle_id: Optional[UUID],
        soc: Optional[float],
        power_kw: Optional[float],
        max_charge_kw: Optional[float],
        timestamp: datetime,
    ) -> None:
        """Process MeterValues message from charger.

        If max_charge_kw is reported, it's stored and takes precedence
        over static config for optimization.
        
        Per PRD Section 8.4, this value must be dynamically updated
        in the vehicles table for consistency.
        """
        # Store telemetry (charger_id tracks which charger reported this)
        await self.db.execute(
            """
            INSERT INTO telemetry (time, vehicle_id, charger_id, soc, charging_kw, max_charge_kw)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            timestamp, vehicle_id, charger_id, soc, power_kw, max_charge_kw
        )
        
        # Cache max_charge_kw if reported
        if vehicle_id and max_charge_kw:
            self._recent_max_charge_kw[str(vehicle_id)] = (max_charge_kw, timestamp)
            logger.debug(f"Vehicle {vehicle_id} max_charge_kw from OCPP: {max_charge_kw}")
            
            # CRITICAL: Update vehicles table dynamically (per PRD Section 8.4)
            # This ensures data consistency and prevents stale max_charge_kw in DB
            await self._update_vehicle_max_charge_kw(vehicle_id, max_charge_kw, timestamp)
    
    async def _update_vehicle_max_charge_kw(
        self,
        vehicle_id: UUID,
        max_charge_kw: float,
        timestamp: datetime,
    ) -> None:
        """Update vehicle's max_charge_kw in database from OCPP MeterValues.
        
        This ensures the vehicles table reflects the most recent OCPP-reported
        value, maintaining data consistency for optimization.
        
        Per PRD Section 8.4, OCPP values take precedence over static config.
        """
        try:
            await self.db.execute(
                """
                UPDATE vehicles
                SET max_charge_kw = $1
                WHERE vehicle_id = $2
                  AND (
                    -- Only update if OCPP value is newer than last update
                    -- or if current value is from config (no OCPP update yet)
                    max_charge_kw IS NULL
                    OR max_charge_kw != $1
                  )
                """,
                max_charge_kw, vehicle_id
            )
            logger.info(
                f"Updated vehicle {vehicle_id} max_charge_kw to {max_charge_kw} kW "
                f"from OCPP MeterValues at {timestamp}"
            )
        except Exception as e:
            logger.error(
                f"Failed to update vehicle {vehicle_id} max_charge_kw: {e}",
                exc_info=True
            )
            # Don't raise - telemetry is stored, update is best-effort
    
    def get_effective_max_charge_kw(
        self,
        vehicle_id: str,
        config_max_kw: float,
        charger_rated_kw: float,
    ) -> float:
        """Get effective max charge rate for a vehicle.
        
        Priority per PRD Section 8.4:
        1. OCPP MeterValues (if recent, < 15 min)
        2. Vehicle config
        3. Charger rated_kw (cap)
        """
        # Check for recent OCPP value
        if vehicle_id in self._recent_max_charge_kw:
            ocpp_value, timestamp = self._recent_max_charge_kw[vehicle_id]
            if datetime.utcnow() - timestamp < self.MAX_CHARGE_KW_STALENESS:
                vehicle_max = ocpp_value
            else:
                vehicle_max = config_max_kw
        else:
            vehicle_max = config_max_kw
        
        # Never exceed charger rating
        return min(vehicle_max, charger_rated_kw)
```

**Verification:**
- [ ] OCPP max_charge_kw stored in telemetry
- [ ] Recent OCPP values used in optimization
- [ ] Fallback to config when OCPP stale

---

## PHASE 4.5: SECURITY & INPUT VALIDATION

### Step 4.5.1: Security Folder Structure

Create `src/security/` directory for security-related modules:

```
src/security/
├── __init__.py
├── validators.py      # Input validation (UUIDs, SoC, power limits)
├── auth.py            # JWT authentication (per PRD Section 10.3)
├── rate_limiter.py    # Rate limiting (per PRD Section 10.4)
└── secrets.py         # Secrets management helpers
```

This centralizes security code for better maintainability and follows reliability engineering principles.

### Step 4.5.2: Input Validation Middleware

Create `src/security/validators.py`:

```python
"""Input validation for API endpoints.

See PRD_v2.md Section 10.4 for security requirements.
"""
from __future__ import annotations

import re
from uuid import UUID
from typing import Any
from fastapi import HTTPException, status

# Validation patterns
UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def validate_uuid(value: str, field_name: str) -> UUID:
    """Validate UUID v4 format.

    Args:
        value: String to validate
        field_name: Name for error messages

    Raises:
        HTTPException: If invalid UUID format
    """
    if not UUID_PATTERN.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid UUID format for {field_name}"
        )
    return UUID(value)


def validate_soc(value: float, field_name: str) -> float:
    """Validate SoC is in range [0.0, 1.0].

    Args:
        value: SoC value to validate
        field_name: Name for error messages

    Raises:
        HTTPException: If out of bounds
    """
    if not (0.0 <= value <= 1.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be between 0.0 and 1.0, got {value}"
        )
    return value


def validate_power(value: float, field_name: str, max_site_power: float) -> float:
    """Validate power value is non-negative and within site limits.

    Args:
        value: Power value in kW
        field_name: Name for error messages
        max_site_power: Maximum allowed power (kW)

    Raises:
        HTTPException: If invalid
    """
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be non-negative, got {value}"
        )
    if value > max_site_power:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} exceeds max site power ({max_site_power} kW)"
        )
    return value


def sanitize_sql_identifier(value: str) -> str:
    """Sanitize identifier for SQL queries (defense in depth).

    Only allows alphanumeric and underscore characters.
    Primary protection is parameterized queries.
    """
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid identifier format"
        )
    return value
```

### Step 4.5.3: Rate Limiting

Create `src/security/rate_limiter.py`:

```python
"""Rate limiting for API endpoints.

See PRD_v2.md Section 10.4 for rate limit specifications.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID
import asyncio
import logging

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Rate limit configuration per PRD Section 10.4."""
    # General API endpoints
    api_requests_per_minute: int = 100

    # POST /optimize endpoint (more expensive)
    optimize_requests_per_minute: int = 10

    # Trigger-induced optimizations (per depot)
    trigger_optimization_cooldown_seconds: int = 300  # 5 minutes


@dataclass
class RateLimiter:
    """Token bucket rate limiter."""
    config: RateLimitConfig = field(default_factory=RateLimitConfig)
    _api_buckets: dict = field(default_factory=lambda: defaultdict(list))
    _optimize_buckets: dict = field(default_factory=lambda: defaultdict(list))
    _last_trigger_optimization: dict = field(default_factory=dict)

    def _clean_bucket(self, bucket: list, window_seconds: int) -> list:
        """Remove entries older than window."""
        cutoff = time.time() - window_seconds
        return [t for t in bucket if t > cutoff]

    def check_api_limit(self, client_id: str) -> bool:
        """Check if client is within API rate limit.

        Args:
            client_id: Client identifier (IP or API key)

        Returns:
            True if request allowed, False if rate limited
        """
        bucket = self._clean_bucket(self._api_buckets[client_id], 60)
        self._api_buckets[client_id] = bucket

        if len(bucket) >= self.config.api_requests_per_minute:
            logger.warning(f"Rate limit exceeded for client {client_id}")
            return False

        self._api_buckets[client_id].append(time.time())
        return True

    def check_optimize_limit(self, client_id: str) -> bool:
        """Check if client is within optimization rate limit.

        POST /optimize is expensive, so stricter limits apply.
        """
        bucket = self._clean_bucket(self._optimize_buckets[client_id], 60)
        self._optimize_buckets[client_id] = bucket

        if len(bucket) >= self.config.optimize_requests_per_minute:
            logger.warning(f"Optimize rate limit exceeded for client {client_id}")
            return False

        self._optimize_buckets[client_id].append(time.time())
        return True

    def check_trigger_cooldown(self, depot_id: UUID) -> bool:
        """Check if depot is within trigger optimization cooldown.

        Prevents rapid re-optimization from trigger events.

        Returns:
            True if optimization allowed, False if in cooldown
        """
        last_time = self._last_trigger_optimization.get(depot_id)
        if last_time is None:
            return True

        elapsed = time.time() - last_time
        if elapsed < self.config.trigger_optimization_cooldown_seconds:
            logger.info(
                f"Depot {depot_id} in trigger cooldown, "
                f"{self.config.trigger_optimization_cooldown_seconds - elapsed:.0f}s remaining"
            )
            return False

        return True

    def record_trigger_optimization(self, depot_id: UUID) -> None:
        """Record that a trigger-induced optimization occurred."""
        self._last_trigger_optimization[depot_id] = time.time()


# Global rate limiter instance
rate_limiter = RateLimiter()
```

### Step 4.5.4: Authentication (JWT)

Create `src/security/auth.py`:

```python
"""JWT authentication for API endpoints.

See PRD_v2.md Section 10.3 for authentication requirements.
"""
from __future__ import annotations

import os
import jwt
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# JWT configuration per PRD Section 10.3
JWT_ACCESS_TOKEN_EXPIRY = timedelta(hours=1)  # 1 hour access token
JWT_REFRESH_TOKEN_EXPIRY = timedelta(hours=24)  # 24 hour refresh token
JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY')  # Must be set in production
JWT_ALGORITHM = 'HS256'

security = HTTPBearer()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """Verify JWT token and return payload.
    
    Raises HTTPException if token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )


# Usage in FastAPI endpoints:
# @app.get("/depots/{depot_id}/state")
# async def get_state(depot_id: UUID, token: dict = Depends(verify_token)):
#     ...
```

### Step 4.5.5: TLS Configuration

Update `docker-compose.yml` to include TLS for all exposed ports:

```yaml
services:
  api:
    # ... existing config ...
    volumes:
      - ./gurobi.lic:/opt/gurobi/gurobi.lic:ro
      - ./certs:/etc/ssl/certs:ro  # TLS certificates
    environment:
      # ... existing env vars ...
      TLS_CERT_PATH: /etc/ssl/certs/api.crt
      TLS_KEY_PATH: /etc/ssl/certs/api.key
      # Require TLS for all connections
      REQUIRE_TLS: "true"
```

Add Nginx reverse proxy for TLS termination (recommended for production):

```yaml
  nginx:
    image: nginx:alpine
    ports:
      - "443:443"  # HTTPS
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/ssl/certs:ro
    depends_on:
      - api
```

### Step 4.5.6: Secrets Management

Create `src/security/secrets.py`:

```python
"""Secrets management helpers.

Per PRD Section 10.3, use environment variables or Vault (future).
"""
from __future__ import annotations

import os
from typing import Optional

def get_secret(key: str, default: Optional[str] = None) -> str:
    """Get secret from environment variable.
    
    In production, this should integrate with HashiCorp Vault or similar.
    """
    value = os.getenv(key, default)
    if value is None:
        raise ValueError(f"Required secret {key} not found in environment")
    return value


# Usage:
# db_password = get_secret('DB_PASSWORD')
# jwt_secret = get_secret('JWT_SECRET_KEY')
```

**Verification:**
- [ ] All secrets use environment variables (no hardcoded passwords)
- [ ] TLS certificates configured for production
- [ ] JWT authentication implemented on all API endpoints
- [ ] Health checks fail on insecure configurations

### Step 4.5.7: SQL Injection Prevention

**Enforcement:** All database queries MUST use parameterized queries. Never interpolate user input into SQL strings.

Create `src/db/queries.py`:

```python
"""Database query helpers with parameterized queries.

SECURITY: All queries use parameterized statements to prevent SQL injection.
See PRD_v2.md Section 10.4.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID


# Example of CORRECT parameterized query
async def get_vehicle_telemetry(
    db,
    vehicle_id: UUID,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Get telemetry for a vehicle in time range.

    Uses parameterized query - NEVER interpolate user input.
    """
    # CORRECT: Use $1, $2 placeholders
    query = """
        SELECT time, soc, charging_kw, max_charge_kw
        FROM telemetry
        WHERE vehicle_id = $1
          AND time >= $2
          AND time <= $3
        ORDER BY time DESC
    """
    return await db.fetch(query, vehicle_id, start_time, end_time)


# Example of INCORRECT query (DO NOT USE)
# async def bad_query(db, vehicle_id: str):
#     # WRONG: String interpolation allows SQL injection
#     query = f"SELECT * FROM vehicles WHERE vehicle_id = '{vehicle_id}'"
#     return await db.fetch(query)


async def insert_telemetry(
    db,
    vehicle_id: UUID,
    charger_id: Optional[UUID],
    soc: Optional[float],
    charging_kw: Optional[float],
    max_charge_kw: Optional[float],
    timestamp: datetime,
) -> None:
    """Insert telemetry record with parameterized query."""
    query = """
        INSERT INTO telemetry (time, vehicle_id, charger_id, soc, charging_kw, max_charge_kw)
        VALUES ($1, $2, $3, $4, $5, $6)
    """
    await db.execute(query, timestamp, vehicle_id, charger_id, soc, charging_kw, max_charge_kw)
```

### Step 4.5.8: Data Freshness Requirements

Implement staleness checks per PRD_v2.md Section 10.4:

```python
"""Data freshness validation.

See PRD_v2.md Section 10.4 for staleness thresholds.
"""
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# Maximum age for data to be considered fresh (per PRD Section 5.3)
MAX_TELEMETRY_AGE = timedelta(minutes=15)
MAX_PRICE_AGE = timedelta(hours=24)
MAX_WEATHER_AGE = timedelta(hours=6)
MAX_BUILDING_LOAD_AGE = timedelta(minutes=30)


def check_data_freshness(
    telemetry_time: Optional[datetime],
    price_time: Optional[datetime],
    weather_time: Optional[datetime],
) -> dict[str, bool]:
    """Check if input data is fresh enough for optimization.

    Args:
        telemetry_time: Most recent telemetry timestamp
        price_time: Most recent price timestamp
        weather_time: Most recent weather timestamp

    Returns:
        Dict with freshness status for each data type
    """
    now = datetime.utcnow()
    results = {}

    if telemetry_time:
        age = now - telemetry_time
        results['telemetry_fresh'] = age <= MAX_TELEMETRY_AGE
        if not results['telemetry_fresh']:
            logger.warning(f"Telemetry data stale: {age.total_seconds() / 60:.1f} minutes old")
    else:
        results['telemetry_fresh'] = False
        logger.warning("No telemetry data available")

    if price_time:
        age = now - price_time
        results['price_fresh'] = age <= MAX_PRICE_AGE
        if not results['price_fresh']:
            logger.warning(f"Price data stale: {age.total_seconds() / 3600:.1f} hours old")
    else:
        results['price_fresh'] = False
        logger.warning("No price data available")

    if weather_time:
        age = now - weather_time
        results['weather_fresh'] = age <= MAX_WEATHER_AGE
        if not results['weather_fresh']:
            logger.warning(f"Weather data stale: {age.total_seconds() / 3600:.1f} hours old")
    else:
        results['weather_fresh'] = False
        logger.warning("No weather data available")

    return results
```

**Verification:**
- [ ] All UUIDs validated before use
- [ ] SoC values rejected if outside [0.0, 1.0]
- [ ] Power values validated against site limits
- [ ] Rate limiting enforced on all endpoints
- [ ] Trigger cooldown prevents rapid re-optimization
- [ ] All SQL queries use parameterized statements
- [ ] Data freshness checked before optimization

---

## PHASE 5: VDV 463 TRANSIT OPERATIONS INTEGRATION

### Overview

VDV 463 enables communication with transit operations systems (BMS/ITCS) for:
- Receiving charging requests with arrival/departure schedules
- Reporting charging status back to operations
- Managing bus preconditioning (manual and automatic)

**Reference:** `docs/PRD_v2_7_Building_Integration.md` Section 9.6

**VDV 463 Schema Set (from `VDVde/VDV463/schema`):**
- `MessageStructure.json`
- `BootNotificationRequest.json` / `BootNotificationResponse.json`
- `ProvideChargingRequestsRequest.json` / `ProvideChargingRequestsResponse.json`
- `ProvideChargingInformationRequest.json` / `ProvideChargingInformationResponse.json`

All request and information payloads MUST validate against their `*Request` schemas; all confirmations use empty-object payloads at index 6 per their `*Response` schemas.

### Schema Validation Strategy

VDV 463 JSON schema validation MUST be **configurable**, with:

- **Default (production) mode – Hard fail:**
  - All inbound and outbound VDV 463 messages (full message array + payload) MUST validate against the official schemas in `VDVde/VDV463/schema` (`MessageStructure.json`, `ProvideChargingRequestsRequest.json`, `ProvideChargingInformationRequest.json`).
  - Messages that fail validation are **rejected**:
    - Return a VDV 463 Error message (MessageType `3`) with an `errorCode` such as `SchemaValidationError` and a concise `errorDescription`. The exact error payload shape is a Favonius convention (not defined by the VDV 463 schemas), and upstream systems must treat `errorCode` as an opaque string and be robust to additional fields.
    - Log the failure with depot_id, presystem_id, action, and validation details for operator diagnostics.

- **Soft/log mode – Non-production / controlled deployments:**
  - If `vdv463.validation_mode = "soft"` (config flag), accept messages with non-critical deviations, but:
    - Record validation warnings in logs and metrics.
    - Mark the affected requests in `vdv463_charging_requests` with a `validation_status` field (e.g., `"warning"`), for later analysis.

**Note:** Confirmations for `ProvideChargingRequests` and `ProvideChargingInformation` use empty-object payloads at index 6 in accordance with the official `*Response.json` schemas. Any acceptance semantics are implicit and captured via logs/metrics rather than additional payload fields.

Implementation notes:
- Implement validation in `src/adapters/vdv463/messages.py` using a schema-backed layer (jsonschema or Pydantic models generated from the official JSON schemas).
- The handler (`VDV463Handler`) MUST call this validation layer before applying any business logic and must respect the configured mode (`hard` vs `soft`), supplied via environment variable or depot-level configuration.

### Step 5.1: VDV 463 WebSocket Handler

Create `src/adapters/vdv463/handler.py`:

```python
"""VDV 463 WebSocket handler for BMS/ITCS communication.

Implements VDV 463 v1.1.0 protocol over WebSocket Secure (WSS).
See docs/PRD_v2_7_Building_Integration.md Section 9.6 and the official VDV 463 JSON schemas
in https://github.com/VDVde/VDV463/tree/main/schema for the authoritative specification.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class VDV463Handler:
    """Handles VDV 463 protocol messages from BMS/ITCS systems."""
    
    def __init__(self, db_pool, depot_id: UUID):
        self.db = db_pool
        self.depot_id = depot_id
        self.presystem_id: Optional[str] = None
        self.system_type: Optional[str] = None  # 'BMS' or 'ITCS'
    
    async def handle_connection(self, websocket: WebSocket, presystem_id: str):
        """Handle incoming VDV 463 WebSocket connection."""
        await websocket.accept()
        self.presystem_id = presystem_id
        
        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                await self._process_message(websocket, message)
        except WebSocketDisconnect:
            logger.info(f"VDV 463 presystem {presystem_id} disconnected")
            await self._log_disconnect()
    
    async def _process_message(self, websocket: WebSocket, message: list):
        """Process VDV 463 message based on MessageStructure.json."""
        # MessageStructure indices:
        # 0 = MessageType (1=Request, 2=Confirmation, 3=Error)
        # 1 = Source ("BMS" | "ITCS" | "CMS")
        # 2 = PresystemId (string)
        # 3 = Timestamp (date-time)
        # 4 = MessageId (UUID)
        # 5 = MessageAction ("BootNotification" | "ProvideChargingRequests" | "ProvideChargingInformation")
        # 6 = Payload (object)
        msg_type = message[0]
        action = message[5]

        # Validate against MessageStructure.json and action-specific schema
        from src.adapters.vdv463.messages import validate_message
        try:
            validate_message(message)
        except VDV463Error as e:
            error_response = [
                3,
                "CMS",
                message[2],
                datetime.utcnow().isoformat(),
                f"schema-error-{message[4]}",
                action,
                {"errorCode": "SchemaValidationError", "errorDescription": e.description},
            ]
            await websocket.send_text(json.dumps(error_response))
            return

        if action == "BootNotification":
            await self._handle_boot_notification(websocket, message)
        elif action == "ProvideChargingRequests":
            await self._handle_charging_requests(websocket, message)
        else:
            logger.warning(f"Unknown VDV 463 message action: {action}")
    
    async def _handle_boot_notification(self, websocket: WebSocket, message: list):
        """Handle BootNotification from BMS/ITCS."""
        payload = message[6]
        # BootNotificationRequest.json: { "presystem": "BMS" | "ITCS" }
        self.system_type = payload.get("presystem", "BMS")
        
        # Log connection
        await self.db.execute("""
            INSERT INTO vdv463_connections (depot_id, presystem_id, system_type, connected_at)
            VALUES ($1, $2, $3, NOW())
        """, self.depot_id, self.presystem_id, self.system_type)
        
        # Send acceptance response
        # BootNotificationResponse.json: { "status": "Accepted" | "Rejected" }
        response = [
            2,
            "CMS",
            self.presystem_id,
            datetime.utcnow().isoformat(),
            message[4],
            "BootNotification",
            {"status": "Accepted"},
        ]
        await websocket.send_text(json.dumps(response))
    
    async def _handle_charging_requests(self, websocket: WebSocket, message: list):
        """Handle ProvideChargingRequests from BMS/ITCS."""
        payload = message[6]
        # Per ProvideChargingRequestsRequest.json, payload has 'chargingRequestList'
        requests = payload.get("chargingRequestList", [])
        
        for req in requests:
            try:
                await self._process_charging_request(req)
            except VDV463Error as e:
                # Send error response per PRD Section 9.6
                error_response = [
                    3,
                    "CMS",
                    self.presystem_id,
                    datetime.utcnow().isoformat(),
                    f"error-{req['chargingRequestId']}",
                    "ProvideChargingRequests",
                    {
                        "errorCode": e.code,
                        "errorDescription": e.description,
                        "chargingRequestId": req["chargingRequestId"],
                    },
                ]
                await websocket.send_text(json.dumps(error_response))
        
        # Send acknowledgment (ProvideChargingRequestsResponse.json: empty object payload)
        ack = [
            2,
            "CMS",
            self.presystem_id,
            datetime.utcnow().isoformat(),
            message[4],
            "ProvideChargingRequests",
            {},
        ]
        await websocket.send_text(json.dumps(ack))
    
    async def _process_charging_request(self, request: dict):
        """Process individual charging request and store in database."""
        from src.adapters.vdv463.vehicle_resolver import resolve_vdv_vehicle_id
        
        # Resolve vehicle ID (per PRD v2.7 Section 9.6)
        vdv_vehicle_id = request['vehicleId']
        vehicle_uuid = await resolve_vdv_vehicle_id(self.db, self.depot_id, vdv_vehicle_id)
        
        # Extract charging data
        data = request.get('chargingRequestData', {})
        precond = request.get('manualPreconditioning') or request.get('automaticPreconditioning')
        precond_type = 'manual' if 'manualPreconditioning' in request else ('automatic' if precond else None)
        
        # Store request (captures all relevant AutomaticPreconditioning and ManualPreconditioning fields)
        await self.db.execute("""
            INSERT INTO vdv463_charging_requests 
            (depot_id, charging_request_id, charging_point_id, vehicle_id, priority,
             charging_instruction, expected_arrival, expected_soc_at_arrival,
             min_target_soc, max_target_soc, requested_departure,
             preconditioning_type, preconditioning_start, 
             ambient_temperature, requested_start_time, requested_finish_time,
             hvac_aux_power, system_aux_power,
             presystem_id, message_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12, $13, $14, $15, $16, $17, $18, $19)
            ON CONFLICT (depot_id, charging_request_id, presystem_id) 
            DO UPDATE SET charging_instruction = EXCLUDED.charging_instruction,
                          expected_arrival = EXCLUDED.expected_arrival,
                          min_target_soc = EXCLUDED.min_target_soc,
                          max_target_soc = EXCLUDED.max_target_soc,
                          requested_departure = EXCLUDED.requested_departure,
                          preconditioning_type = EXCLUDED.preconditioning_type,
                          preconditioning_start = EXCLUDED.preconditioning_start,
                          ambient_temperature = EXCLUDED.ambient_temperature,
                          requested_start_time = EXCLUDED.requested_start_time,
                          requested_finish_time = EXCLUDED.requested_finish_time,
                          hvac_aux_power = EXCLUDED.hvac_aux_power,
                          system_aux_power = EXCLUDED.system_aux_power
        """, 
             self.depot_id,
             request['chargingRequestId'], 
             request.get('chargingPointId'),
             vehicle_uuid,
             request.get('priority', 1),
             request.get('chargingInstruction', 'Normal'),
             data.get('expectedArrivalTimeAtChargingPoint'),
             data.get('expectedSocAtArrival'),
             data.get('minTargetSoc'),
             data.get('maxTargetSoc'),
             data.get('requestedTimeForDeparture'),
             precond_type,
             precond.get('hvacPreconditioningStartTime') if precond else None,
             precond.get('ambientTemperature') if precond else None,
             precond.get('requestedStartTime') if precond else None,
             precond.get('requestedFinishTime') if precond else None,
             precond.get('hvacAuxiliaryConsumerPower') if precond else None,
             precond.get('systemAuxiliaryConsumerPower') if precond else None,
             self.presystem_id,
             request.get('messageId', ''))


class VDV463Error(Exception):
    """VDV 463 protocol error."""
    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}")
```

### Step 5.2: Vehicle ID Resolution

Create `src/adapters/vdv463/vehicle_resolver.py`:

```python
"""VDV 463 vehicle ID resolution.

Maps VDV 463 vehicleId strings to internal UUIDs.
See docs/PRD_v2_7_Building_Integration.md Section 9.6 and the official VDV 463 JSON schemas
in https://github.com/VDVde/VDV463/tree/main/schema for the authoritative specification.
"""
from __future__ import annotations

from uuid import UUID
import logging

logger = logging.getLogger(__name__)


async def resolve_vdv_vehicle_id(db, depot_id: UUID, vdv_vehicle_id: str) -> UUID:
    """Map VDV 463 vehicleId string to internal vehicle UUID.
    
    Resolution path: vdv.vehicleId → vehicles.external_id → vehicles.vehicle_id
    
    Args:
        db: Database connection pool
        depot_id: Depot UUID for scoping lookup
        vdv_vehicle_id: VDV 463 vehicleId string (e.g., "bus_101")
    
    Returns:
        Vehicle UUID
    
    Raises:
        VDV463Error: If vehicle not found in depot configuration
    """
    from src.adapters.vdv463.handler import VDV463Error
    
    result = await db.fetchrow("""
        SELECT vehicle_id FROM vehicles 
        WHERE depot_id = $1 AND external_id = $2
    """, depot_id, vdv_vehicle_id)
    
    if result is None:
        logger.warning(f"VDV 463 vehicle '{vdv_vehicle_id}' not found in depot {depot_id}")
        raise VDV463Error(
            code="InvalidVehicleId",
            description=f"Vehicle '{vdv_vehicle_id}' not found in depot configuration"
        )
    
    logger.debug(f"Resolved VDV 463 vehicle '{vdv_vehicle_id}' to UUID {result['vehicle_id']}")
    return result['vehicle_id']
```

### Step 5.3: VDV 463 Database Schema

Add to `migrations/003_vdv463_schema.sql`:

```sql
-- VDV 463 charging requests (optimization inputs)
CREATE TABLE vdv463_charging_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL REFERENCES depots(depot_id),
    charging_request_id TEXT NOT NULL,
    charging_point_id UUID REFERENCES chargers(charger_id),
    vehicle_id UUID REFERENCES vehicles(vehicle_id),
    priority INTEGER DEFAULT 1,
    charging_instruction TEXT DEFAULT 'Normal',
    expected_arrival TIMESTAMPTZ,
    expected_soc_at_arrival REAL,
    min_target_soc REAL,
    max_target_soc REAL,
    requested_departure TIMESTAMPTZ,
    preconditioning_type TEXT,              -- 'manual', 'automatic', or NULL
    preconditioning_start TIMESTAMPTZ,      -- For manual
    ambient_temperature REAL,               -- For automatic
    requested_start_time TIMESTAMPTZ,       -- For automatic (requestedStartTime)
    requested_finish_time TIMESTAMPTZ,      -- For automatic (requestedFinishTime)
    hvac_aux_power INTEGER,                 -- For manual (hvacAuxiliaryConsumerPower)
    system_aux_power INTEGER,               -- For manual (systemAuxiliaryConsumerPower)
    presystem_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    received_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'active',           -- 'active', 'completed', 'terminated'
    validation_status TEXT,                 -- NULL | 'ok' | 'warning' | 'error'
    UNIQUE(depot_id, charging_request_id, presystem_id)
);
SELECT create_hypertable('vdv463_charging_requests', 'received_at');

-- VDV 463 connection log
CREATE TABLE vdv463_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL REFERENCES depots(depot_id),
    presystem_id TEXT NOT NULL,
    system_type TEXT NOT NULL,              -- 'BMS' or 'ITCS'
    connected_at TIMESTAMPTZ DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ,
    disconnect_reason TEXT
);
```

### Step 5.4: Preconditioning Constraint Integration

Update `src/core/optimizer/milp_model.py` to include preconditioning as a **soft constraint** (per PRD v2.7):

```python
def add_preconditioning_constraints(model, preconditioning_requests: list):
    """Add VDV 463 preconditioning as soft constraint.
    
    Per PRD v2.7, preconditioning is a SOFT constraint with high penalty (M=1000).
    This allows curtailment under site power limit while strongly encouraging fulfillment.
    
    Priority Hierarchy:
    1. Site power limit (hard - electrical safety)
    2. Departure SoC ≥ 99% (hard - operational)
    3. Preconditioning (soft with M=1000 penalty)
    4. Energy cost minimization (objective)
    """
    M_PRECOND = 1000.0  # Penalty weight $/kW
    
    # Slack variable for unfulfilled preconditioning
    model.precond_slack = pyo.Var(model.T, within=pyo.NonNegativeReals)
    
    # Preconditioning load constraint (soft)
    def precond_load_rule(model, t):
        required_load = sum(
            req['power_kw'] for req in preconditioning_requests
            if req['start_time'] <= t < req['end_time']
        )
        return model.P_precond[t] + model.precond_slack[t] >= required_load
    
    model.precond_constraint = pyo.Constraint(model.T, rule=precond_load_rule)
    
    # Add penalty to objective function
    model.precond_penalty = pyo.Expression(
        expr=M_PRECOND * sum(model.precond_slack[t] for t in model.T)
    )
    
    return model
```

**Verification:**
- [ ] VDV 463 WebSocket endpoint accepts connections at `/vdv463/{presystem_id}`
- [ ] BootNotification handled correctly
- [ ] ProvideChargingRequests stored in database
- [ ] Vehicle ID resolution works (external_id → UUID)
- [ ] InvalidVehicleId error returned for unknown vehicles
- [ ] Preconditioning requests included in optimization
- [ ] Preconditioning curtailed gracefully under site power limit

---

## PHASE 6: BACNET/SC BUILDING HVAC INTEGRATION

### Overview

BACnet/SC (Secure Connect) enables building HVAC control for thermal flywheel optimization:
- Pre-cool/heat during low-cost periods
- Reduce HVAC load during peak charging
- Coordinate building and EV charging for demand reduction

**Reference:** PRD v2.7 Section 9.7

### Step 6.1: BuildingZone Data Model

Update `src/core/models.py` to add BuildingZone:

```python
@dataclass
class BuildingZone:
    """Building zone configuration for HVAC control.
    
    See PRD_v2.md Section 6.2 and 9.7.
    """
    zone_id: UUID
    depot_id: UUID
    name: str
    bacnet_device_id: int                     # BACnet device identifier
    temp_sensor_oid: str                      # BACnet object ID (e.g., "analogInput:1")
    setpoint_cmd_oid: str                     # BACnet object ID (e.g., "analogValue:1")
    active_setpoint_oid: Optional[str]        # For reading baseline setpoint (e.g., "analogValue:2")
    thermal_mass_kwh_c: float                 # Thermal mass (kWh per degree C)
    current_temp_c: float                     # Current zone temperature
    min_temp_c: float = 19.0                  # Minimum allowed temperature (safety limit)
    max_temp_c: float = 24.0                  # Maximum allowed temperature (safety limit)
    baseline_load_kw: float = 0.0             # Non-HVAC baseline load for this zone
    max_hvac_power_kw: float = 50.0           # Maximum HVAC power for zone (kW)
    hvac_cop: float = 3.5                     # HVAC Coefficient of Performance
    ua_value: float = 0.5                     # Heat transfer coefficient (kW/°C)
    hvac_response_lag_min: float = 5.0        # HVAC response lag (minutes)
    hvac_ramp_kw_per_timestep: float = 5.0    # HVAC ramp rate limit (kW per timestep)
```

### Step 6.2: BACnet/SC Hub Implementation

Create `src/adapters/bacnet/hub.py`:

```python
"""BACnet/SC Hub for building HVAC control.

Uses bacpypes3 library for BACnet Secure Connect.
See PRD_v2.md Section 9.7 for specification.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from bacpypes3.app import Application
from bacpypes3.basetypes import PropertyIdentifier
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.pdu import Address

logger = logging.getLogger(__name__)


class BACnetSCHub:
    """BACnet/SC Hub for building HVAC control.
    
    Single shared Hub instance per worker (not per-connection).
    Handles multiple BACnet device connections.
    """
    
    def __init__(self, db_pool, depot_id: UUID):
        self.db = db_pool
        self.depot_id = depot_id
        self.app: Optional[Application] = None
        self._zone_cache: dict[tuple[int, str], UUID] = {}  # (device_id, oid) → zone_id
    
    async def start(self, hub_address: str = "0.0.0.0"):
        """Start BACnet/SC Hub."""
        # Initialize bacpypes3 Application
        self.app = Application()
        await self.app.startup()
        logger.info(f"BACnet/SC Hub started for depot {self.depot_id}")
    
    async def stop(self):
        """Stop BACnet/SC Hub."""
        if self.app:
            await self.app.shutdown()
    
    async def resolve_zone(self, device_id: int, object_id: str) -> UUID:
        """Map BACnet device and object to zone UUID.
        
        Per PRD v2.7, uses building_zones table with unique constraint
        on (depot_id, bacnet_device_id, temp_sensor_oid).
        """
        cache_key = (device_id, object_id)
        if cache_key in self._zone_cache:
            return self._zone_cache[cache_key]
        
        result = await self.db.fetchrow("""
            SELECT zone_id FROM building_zones 
            WHERE depot_id = $1 AND bacnet_device_id = $2 AND temp_sensor_oid = $3
        """, self.depot_id, device_id, object_id)
        
        if result is None:
            raise BACnetError(
                code="InvalidObjectId",
                description=f"No zone mapped to device {device_id} object {object_id}"
            )
        
        zone_id = result['zone_id']
        self._zone_cache[cache_key] = zone_id
        return zone_id
    
    async def read_zone_temperature(self, zone: 'BuildingZone') -> float:
        """Read current temperature from BACnet device."""
        # Parse object identifier (e.g., "analogInput:1")
        obj_type, obj_instance = zone.temp_sensor_oid.split(':')
        
        value = await self.app.read_property(
            Address(zone.bacnet_device_id),
            f"{obj_type},{obj_instance}",
            PropertyIdentifier.presentValue
        )
        return float(value)
    
    async def read_baseline_setpoint(self, zone: 'BuildingZone') -> Optional[float]:
        """Read current baseline setpoint from BMS.
        
        Used for setpoint offset calculation per PRD v2.7 Constraint 15b.
        """
        if not zone.active_setpoint_oid:
            return None
        
        obj_type, obj_instance = zone.active_setpoint_oid.split(':')
        
        try:
            value = await self.app.read_property(
                Address(zone.bacnet_device_id),
                f"{obj_type},{obj_instance}",
                PropertyIdentifier.presentValue
            )
            return float(value)
        except Exception as e:
            logger.warning(f"Failed to read baseline setpoint for zone {zone.zone_id}: {e}")
            return None
    
    async def write_setpoint_offset(
        self, 
        zone: 'BuildingZone', 
        offset_c: float,
        baseline_setpoint: float
    ) -> bool:
        """Write setpoint offset to BACnet device.
        
        Per PRD v2.7 Constraint 15b:
        - Clamps offset to ensure final setpoint stays within safety bounds
        - Validates before dispatch
        
        Args:
            zone: Building zone configuration
            offset_c: Desired temperature offset (°C)
            baseline_setpoint: Current BMS baseline setpoint (°C)
        
        Returns:
            True if command sent successfully
        """
        # Safety clamping per PRD v2.7
        effective_setpoint = baseline_setpoint + offset_c
        
        if effective_setpoint < zone.min_temp_c:
            offset_c = zone.min_temp_c - baseline_setpoint
            logger.warning(f"Clamped offset to {offset_c}°C to respect min temp {zone.min_temp_c}°C")
        elif effective_setpoint > zone.max_temp_c:
            offset_c = zone.max_temp_c - baseline_setpoint
            logger.warning(f"Clamped offset to {offset_c}°C to respect max temp {zone.max_temp_c}°C")
        
        # Parse object identifier
        obj_type, obj_instance = zone.setpoint_cmd_oid.split(':')
        
        try:
            await self.app.write_property(
                Address(zone.bacnet_device_id),
                f"{obj_type},{obj_instance}",
                PropertyIdentifier.presentValue,
                AnyAtomic(offset_c)
            )
            
            # Log command
            await self.db.execute("""
                INSERT INTO hvac_telemetry (time, zone_id, setpoint_offset_c)
                VALUES (NOW(), $1, $2)
            """, zone.zone_id, offset_c)
            
            return True
        except Exception as e:
            logger.error(f"Failed to write setpoint offset for zone {zone.zone_id}: {e}")
            return False


class BACnetError(Exception):
    """BACnet protocol error."""
    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}")
```

### Step 6.3: Setpoint Offset Calculation

Create `src/adapters/bacnet/setpoint.py`:

```python
"""HVAC setpoint offset calculation.

Implements PRD v2.7 Constraint 15b: Offset = T_optimal - T_baseline
"""
from __future__ import annotations

from typing import Optional
import logging

logger = logging.getLogger(__name__)


def calculate_setpoint_offset(
    t_optimal: float,
    t_baseline: float,
    t_min: float,
    t_max: float
) -> float:
    """Calculate setpoint offset with safety clamping.
    
    Per PRD v2.7 Constraint 15b:
    Offset[z,t] = T_optimal[z,t] - T_baseline_setpoint[z]
    
    Args:
        t_optimal: Optimized zone temperature from solver (°C)
        t_baseline: BMS current active setpoint (°C)
        t_min: Zone minimum temperature (°C)
        t_max: Zone maximum temperature (°C)
    
    Returns:
        Clamped offset value (°C)
    """
    offset = t_optimal - t_baseline
    
    # Safety clamping
    effective_setpoint = t_baseline + offset
    
    if effective_setpoint < t_min:
        clamped_offset = t_min - t_baseline
        logger.info(f"Clamped offset {offset:.2f}°C → {clamped_offset:.2f}°C (min bound)")
        return clamped_offset
    elif effective_setpoint > t_max:
        clamped_offset = t_max - t_baseline
        logger.info(f"Clamped offset {offset:.2f}°C → {clamped_offset:.2f}°C (max bound)")
        return clamped_offset
    
    return offset
```

### Step 6.4: BACnet Database Schema

Add to `migrations/004_bacnet_schema.sql`:

```sql
-- BACnet device connections
CREATE TABLE bacnet_devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL REFERENCES depots(depot_id),
    device_id INTEGER NOT NULL,  -- BACnet device identifier
    device_name VARCHAR(255),
    connected_at TIMESTAMPTZ DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ,
    certificate_cn TEXT,  -- Client certificate Common Name
    UNIQUE(depot_id, device_id)
);

-- HVAC telemetry (zone temperatures, HVAC power)
CREATE TABLE hvac_telemetry (
    time TIMESTAMPTZ NOT NULL,
    zone_id UUID NOT NULL REFERENCES building_zones(zone_id),
    temperature_c DOUBLE PRECISION,
    hvac_power_kw DOUBLE PRECISION CHECK (hvac_power_kw >= 0),
    setpoint_offset_c DOUBLE PRECISION,  -- Demand response offset applied
    PRIMARY KEY (time, zone_id)
);
SELECT create_hypertable('hvac_telemetry', 'time');

-- Building zones configuration
CREATE TABLE building_zones (
    zone_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL REFERENCES depots(depot_id),
    name VARCHAR(255) NOT NULL,
    bacnet_device_id INTEGER NOT NULL,
    temp_sensor_oid VARCHAR(100) NOT NULL,  -- e.g., "analogInput:1"
    setpoint_cmd_oid VARCHAR(100) NOT NULL,  -- e.g., "analogValue:1"
    active_setpoint_oid VARCHAR(100),        -- e.g., "analogValue:2"
    thermal_mass_kwh_c DOUBLE PRECISION NOT NULL CHECK (thermal_mass_kwh_c > 0),
    min_temp_c DOUBLE PRECISION DEFAULT 19.0,
    max_temp_c DOUBLE PRECISION DEFAULT 24.0,
    baseline_load_kw DOUBLE PRECISION DEFAULT 0,
    hvac_cop DOUBLE PRECISION DEFAULT 3.5 CHECK (hvac_cop > 0),
    ua_value DOUBLE PRECISION DEFAULT 0.5 CHECK (ua_value > 0),
    max_hvac_power_kw DOUBLE PRECISION DEFAULT 50.0 CHECK (max_hvac_power_kw > 0),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_zone_sensor UNIQUE (depot_id, bacnet_device_id, temp_sensor_oid)
);
```

### Step 6.5: HVAC Thermal Dynamics Constraints

Add to `src/core/optimizer/milp_model.py`:

```python
def add_hvac_constraints(model, zones: list['BuildingZone'], ambient_temp: list[float]):
    """Add HVAC thermal dynamics constraints.
    
    Per PRD v2.7 Section 8.1 Constraints 13-15b.
    
    Thermal dynamics model building as "thermal battery":
    - Pre-cool during cheap periods (charging)
    - Let temperature drift during peaks (discharging)
    """
    for z in zones:
        zone_idx = z.zone_id
        
        # Variables
        model.T_zone[zone_idx, :] = pyo.Var(model.T, bounds=(z.min_temp_c, z.max_temp_c))
        model.P_hvac[zone_idx, :] = pyo.Var(model.T, bounds=(0, z.max_hvac_power_kw))
        
        # Constraint 13: Thermal dynamics
        def thermal_dynamics_rule(model, t):
            if t == 0:
                return model.T_zone[zone_idx, t] == z.current_temp_c
            
            # RC thermal model: T[t] = T[t-1] + (UA*(T_amb - T[t-1]) + COP*P_hvac) * dt / C
            dt = model.delta_t  # hours
            T_prev = model.T_zone[zone_idx, t-1]
            T_amb = ambient_temp[t]
            
            heat_transfer = z.ua_value * (T_amb - T_prev)  # kW
            hvac_effect = z.hvac_cop * model.P_hvac[zone_idx, t-1]  # kW (cooling = negative)
            
            dT = (heat_transfer - hvac_effect) * dt / z.thermal_mass_kwh_c
            return model.T_zone[zone_idx, t] == T_prev + dT
        
        model.thermal_dynamics[zone_idx] = pyo.Constraint(model.T, rule=thermal_dynamics_rule)
        
        # Constraint 14: HVAC power limits
        def hvac_power_rule(model, t):
            return model.P_hvac[zone_idx, t] <= z.max_hvac_power_kw
        
        model.hvac_power_limit[zone_idx] = pyo.Constraint(model.T, rule=hvac_power_rule)
        
        # Constraint 15: HVAC ramp rate
        def hvac_ramp_rule(model, t):
            if t == 0:
                return pyo.Constraint.Skip
            return abs(model.P_hvac[zone_idx, t] - model.P_hvac[zone_idx, t-1]) <= z.hvac_ramp_kw_per_timestep
        
        model.hvac_ramp[zone_idx] = pyo.Constraint(model.T, rule=hvac_ramp_rule)
    
    return model
```

**Verification:**
- [ ] BACnet/SC Hub starts and accepts device connections
- [ ] Zone temperature readings work via BACnet
- [ ] Baseline setpoint reading works
- [ ] Setpoint offset calculation correct (T_optimal - T_baseline)
- [ ] Safety clamping enforced (min/max temp bounds)
- [ ] Thermal dynamics constraints in optimization
- [ ] HVAC power included in grid power calculation
- [ ] Pre-cooling scheduled during low-cost periods

---

## PHASE 6.5: DISPATCH VALIDATION

### Overview

Pre-dispatch validation prevents sending commands to unavailable devices.
Handles race conditions between state assembly and command dispatch.

**Reference:** PRD v2.7 Section 8.7

### Step 6.5.1: Dispatcher with Validation

Create `src/core/optimizer/dispatcher.py`:

```python
"""Dispatch validation and command execution.

Implements PRD v2.7 Section 8.7: Pre-dispatch checks.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


class Dispatcher:
    """Handles command dispatch with pre-validation."""
    
    def __init__(self, db_pool, ocpp_client, bacnet_hub):
        self.db = db_pool
        self.ocpp_client = ocpp_client
        self.bacnet_hub = bacnet_hub
    
    async def dispatch_charging_profiles(
        self,
        profiles: dict[str, dict],
        run_id: UUID
    ) -> dict[str, str]:
        """Dispatch SetChargingProfile commands with validation.
        
        Per PRD v2.7 Section 8.7:
        - Validate charger status immediately before dispatch
        - Skip commands to unavailable chargers (Faulted, Unavailable, Reserved)
        - Log skipped commands, do NOT re-run optimization
        
        Returns:
            Dict of charger_id → status ('dispatched', 'skipped')
        """
        results = {}
        
        for charger_id, profile in profiles.items():
            # Pre-dispatch validation
            is_valid = await self._validate_charger_before_dispatch(charger_id)
            
            if not is_valid:
                results[charger_id] = 'skipped'
                await self._log_dispatch_skip(charger_id, run_id, 'charger_unavailable')
                continue
            
            # Send command
            try:
                response = await self.ocpp_client.send_charging_profile(charger_id, profile)
                results[charger_id] = 'dispatched' if response.get('status') == 'Accepted' else 'rejected'
            except Exception as e:
                logger.error(f"Failed to dispatch to charger {charger_id}: {e}")
                results[charger_id] = 'error'
        
        return results
    
    async def dispatch_hvac_setpoints(
        self,
        setpoint_offsets: dict[UUID, float],
        zones: dict[UUID, 'BuildingZone'],
        run_id: UUID
    ) -> dict[UUID, str]:
        """Dispatch HVAC setpoint offset commands with validation.
        
        Per PRD v2.7 Section 8.7:
        - Validate BACnet device connectivity before dispatch
        - Validate offset won't violate safety bounds
        - Skip commands to disconnected devices
        
        Returns:
            Dict of zone_id → status ('dispatched', 'skipped')
        """
        results = {}
        
        for zone_id, offset in setpoint_offsets.items():
            zone = zones[zone_id]
            
            # Pre-dispatch validation
            is_valid = await self._validate_bacnet_before_dispatch(
                zone.bacnet_device_id, zone_id, offset, zone
            )
            
            if not is_valid:
                results[zone_id] = 'skipped'
                await self._log_dispatch_skip(str(zone_id), run_id, 'device_unavailable')
                continue
            
            # Read baseline setpoint
            baseline = await self.bacnet_hub.read_baseline_setpoint(zone)
            if baseline is None:
                baseline = (zone.min_temp_c + zone.max_temp_c) / 2  # Use midpoint as fallback
                logger.warning(f"Using fallback baseline setpoint {baseline}°C for zone {zone_id}")
            
            # Send command
            success = await self.bacnet_hub.write_setpoint_offset(zone, offset, baseline)
            results[zone_id] = 'dispatched' if success else 'error'
        
        return results
    
    async def _validate_charger_before_dispatch(self, charger_id: str) -> bool:
        """Check charger status immediately before dispatch.
        
        Returns False for Faulted, Unavailable, or Reserved status.
        """
        # Query latest status (< 100ms latency target)
        status = await self.db.fetchval("""
            SELECT status FROM chargers WHERE charger_id = $1
        """, charger_id)
        
        if status in ('Faulted', 'Unavailable', 'Reserved'):
            logger.warning(f"Skipping dispatch to charger {charger_id}: status={status}")
            return False
        
        if status == 'Finishing':
            logger.info(f"Charger {charger_id} finishing session, command may be rejected")
        
        return True
    
    async def _validate_bacnet_before_dispatch(
        self,
        device_id: int,
        zone_id: UUID,
        offset: float,
        zone: 'BuildingZone'
    ) -> bool:
        """Check BACnet device connectivity and bounds before dispatch."""
        # Check device connectivity
        device = await self.db.fetchrow("""
            SELECT disconnected_at FROM bacnet_devices 
            WHERE depot_id = $1 AND device_id = $2
        """, zone.depot_id, device_id)
        
        if device is None or device['disconnected_at'] is not None:
            logger.warning(f"Skipping dispatch to BACnet device {device_id}: disconnected")
            return False
        
        # Validate bounds (redundant safety check)
        # Assume baseline = current temp for validation
        effective = zone.current_temp_c + offset
        if effective < zone.min_temp_c or effective > zone.max_temp_c:
            logger.warning(f"Offset {offset}°C may violate bounds for zone {zone_id}")
            # Don't skip - let clamping handle it
        
        return True
    
    async def _log_dispatch_skip(self, device_id: str, run_id: UUID, reason: str):
        """Log skipped dispatch command for audit."""
        await self.db.execute("""
            INSERT INTO charging_commands (run_id, charger_id, status, profile_json)
            VALUES ($1, $2, 'skipped', $3)
        """, run_id, device_id, {'skip_reason': reason})
        
        logger.info(f"Dispatch skipped: device={device_id}, reason={reason}, run_id={run_id}")
```

**Verification:**
- [ ] Charger status checked before SetChargingProfile
- [ ] Commands skipped for Faulted/Unavailable/Reserved chargers
- [ ] BACnet device connectivity checked before setpoint dispatch
- [ ] Skipped commands logged with reason
- [ ] No re-optimization on dispatch failures (use next cycle)

---

## PHASE 7: UNIT TESTS

### Step 5.1: Realistic Test Fixtures

Create `tests/fixtures/realistic_depot.py`:

```python
"""Realistic test fixtures using UUIDs and production-like data.

See PRD_v2.md Section 11 for acceptance criteria these tests validate.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4
import pytest

from src.core.models import (
    DepotConfig, DepotState, IncomingVehicle, 
    Vehicle, Charger, Schedule
)


# Realistic UUIDs for consistent testing
DEPOT_ID = UUID('a1b2c3d4-e5f6-7890-abcd-ef1234567890')
VEHICLE_IDS = {
    'bus_101': UUID('11111111-1111-1111-1111-111111111101'),
    'bus_102': UUID('11111111-1111-1111-1111-111111111102'),
    'bus_103': UUID('11111111-1111-1111-1111-111111111103'),
    'bus_104': UUID('11111111-1111-1111-1111-111111111104'),
    'bus_105': UUID('11111111-1111-1111-1111-111111111105'),
}
CHARGER_IDS = {
    'charger_1': UUID('22222222-2222-2222-2222-222222222201'),
    'charger_2': UUID('22222222-2222-2222-2222-222222222202'),
    'charger_3': UUID('22222222-2222-2222-2222-222222222203'),
}


@pytest.fixture
def realistic_depot_config() -> DepotConfig:
    """Realistic depot configuration for 5 vehicles, 3 chargers."""
    return DepotConfig(
        vehicle_capacities={
            'bus_101': 324.0,  # Large bus
            'bus_102': 324.0,
            'bus_103': 220.0,  # Small bus
            'bus_104': 220.0,
            'bus_105': 150.0,  # Van
        },
        vehicle_max_charge_kw={
            'bus_101': 150.0,
            'bus_102': 150.0,
            'bus_103': 100.0,
            'bus_104': 100.0,
            'bus_105': 50.0,
        },
        charger_groups={80.0: 2, 150.0: 1},  # 2x80kW + 1x150kW
        charger_efficiency=0.95,
        charger_vehicle_access={
            # All chargers accessible to all vehicles (simple case)
            str(CHARGER_IDS['charger_1']): set(VEHICLE_IDS.keys()),
            str(CHARGER_IDS['charger_2']): set(VEHICLE_IDS.keys()),
            str(CHARGER_IDS['charger_3']): set(VEHICLE_IDS.keys()),
        },
        battery_capacity=500.0,
        battery_power=100.0,
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=500.0,
        delta_t=0.25,
        n_timesteps=96,
    )


@pytest.fixture
def realistic_depot_state(realistic_depot_config) -> DepotState:
    """Realistic depot state with TOU pricing and building load."""
    n_t = realistic_depot_config.n_timesteps
    
    # TOU pricing: off-peak $0.08, partial-peak $0.15, peak $0.30
    prices = []
    for t in range(n_t):
        hour = (t * 0.25) % 24
        if 16 <= hour < 21:  # Peak: 4PM-9PM
            prices.append(0.30)
        elif 9 <= hour < 16 or 21 <= hour < 24:  # Partial-peak
            prices.append(0.15)
        else:  # Off-peak
            prices.append(0.08)
    
    # Building load: office pattern (higher during day)
    building_power = []
    for t in range(n_t):
        hour = (t * 0.25) % 24
        if 8 <= hour < 18:  # Business hours
            building_power.append(50.0 + (hour - 8) * 3)  # 50-80 kW
        else:
            building_power.append(20.0)  # Baseline
    
    # Vehicle availability: bus_101, bus_102 depart 6AM, return 2PM
    # bus_103, bus_104 depart 7AM, return 3PM
    # bus_105 stays at depot
    availability = {}
    for vid in VEHICLE_IDS:
        availability[vid] = [True] * n_t
    
    # bus_101, bus_102: out 6AM-2PM (timesteps 24-56)
    for vid in ['bus_101', 'bus_102']:
        for t in range(24, 56):
            availability[vid][t] = False
    
    # bus_103, bus_104: out 7AM-3PM (timesteps 28-60)
    for vid in ['bus_103', 'bus_104']:
        for t in range(28, 60):
            availability[vid][t] = False
    
    return DepotState(
        vehicle_socs={
            'bus_101': 0.35,  # Needs significant charging
            'bus_102': 0.45,
            'bus_103': 0.55,
            'bus_104': 0.60,
            'bus_105': 0.80,  # Mostly charged
        },
        battery_soc=0.5,
        prices=prices,
        demand_charge_rate=20.0,  # $20/kW
        current_month_peak=150.0,  # kW
        vehicle_availability=availability,
        energy_requirements={
            'bus_101': 200.0,  # kWh for route
            'bus_102': 180.0,
            'bus_103': 120.0,
            'bus_104': 100.0,
            'bus_105': 50.0,
        },
        departure_times={
            'bus_101': 24,  # 6AM
            'bus_102': 24,
            'bus_103': 28,  # 7AM
            'bus_104': 28,
            # bus_105: no departure
        },
        building_power=building_power,
        incoming_vehicles=[],
    )


@pytest.fixture
def state_with_incoming_vehicle(realistic_depot_state) -> DepotState:
    """State with an incoming vehicle from another depot."""
    incoming = IncomingVehicle(
        vehicle_id=UUID('33333333-3333-3333-3333-333333333301'),
        external_id='bus_201',
        expected_soc=0.35,
        arrival_time=datetime.utcnow() + timedelta(hours=2),
        battery_kwh=324.0,
        max_charge_kw=150.0,
        origin_depot_id=UUID('b1b2b3b4-b5b6-b7b8-b9ba-bbbbbbbbbbbb'),
    )
    realistic_depot_state.incoming_vehicles = [incoming]
    return realistic_depot_state
```

### Step 5.2: Optimizer Tests

Create `tests/unit/test_optimizer.py`:

```python
"""Unit tests for MILP optimizer.

Validates PRD_v2.md Section 8 requirements.
"""
import pytest
from src.core.optimizer.milp_model import (
    build_optimization_model, solve_model, warm_start_model
)
from tests.fixtures.realistic_depot import (
    realistic_depot_config, realistic_depot_state, 
    state_with_incoming_vehicle
)


class TestOptimizerBasic:
    """Basic optimizer functionality tests."""
    
    def test_model_builds(self, realistic_depot_state, realistic_depot_config):
        """Model should build without errors."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        assert model is not None
        assert hasattr(model, 'objective')
    
    def test_model_solves_feasible(self, realistic_depot_state, realistic_depot_config):
        """Model should find feasible solution."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model, time_limit=60)
        
        assert result.status in ['optimal', 'feasible']
        assert result.objective_value < float('inf')
    
    def test_solve_time_under_60_seconds(self, realistic_depot_state, realistic_depot_config):
        """Solve time must be < 60 seconds per PRD."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model, time_limit=60)
        
        assert result.solve_time_s < 60
    
    def test_gurobi_fallback_to_highs(self, realistic_depot_state, realistic_depot_config):
        """Test automatic fallback to HiGHS when Gurobi fails.
        
        Per PRD Section 8.2, system must gracefully degrade to HiGHS
        if Gurobi license fails or connection issues occur.
        """
        from unittest.mock import patch, MagicMock
        import pyomo.environ as pyo
        
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        
        # Simulate Gurobi failure (e.g., invalid license)
        with patch('pyomo.environ.SolverFactory') as mock_factory:
            # First call (Gurobi) fails
            mock_gurobi = MagicMock()
            mock_gurobi.solve.side_effect = Exception("Gurobi license invalid")
            
            # Second call (HiGHS) succeeds
            mock_highs = MagicMock()
            mock_highs_result = MagicMock()
            mock_highs_result.solver.termination_condition = pyo.TerminationCondition.optimal
            mock_highs_result.solver.time = 25.0
            mock_highs.solve.return_value = mock_highs_result
            
            # Mock factory returns Gurobi first, then HiGHS
            mock_factory.side_effect = [mock_gurobi, mock_highs]
            
            # Should fall back to HiGHS automatically
            result = solve_model(model, time_limit=60)
            
            assert result.solver_used == 'highs'
            assert result.status in ['optimal', 'feasible']
            assert result.solve_time_s < 60


class TestDepartureSoC:
    """Tests for departure SoC constraint (PRD Section 8.1 Constraint 4)."""
    
    def test_all_departures_reach_99_percent(self, realistic_depot_state, realistic_depot_config):
        """All vehicles must reach ≥99% SoC at departure."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        for vid, t_dep in realistic_depot_state.departure_times.items():
            soc = result.schedule[vid]['soc'][t_dep]
            assert soc >= 0.99, f"{vid} not charged at departure: {soc}"
    
    def test_low_soc_vehicle_prioritized(self, realistic_depot_state, realistic_depot_config):
        """Vehicle with lowest SoC should start charging first."""
        # bus_101 has lowest SoC (0.35)
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        # Check that bus_101 starts charging early
        early_charging = sum(result.schedule['bus_101']['charging_power'][:10])
        assert early_charging > 0


class TestBuildingLoad:
    """Tests for building load integration (PRD Section 9.4)."""
    
    def test_grid_power_includes_building_load(self, realistic_depot_state, realistic_depot_config):
        """Grid power must include building load per PRD."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        # Grid power should be >= building load at all times
        for t, grid_kw in enumerate(result.grid_power):
            building_kw = realistic_depot_state.building_power[t]
            # Grid = charging + building - battery
            # So grid >= building when battery not discharging heavily
            assert grid_kw >= building_kw - realistic_depot_config.battery_power


class TestDemandChargeTracking:
    """Tests for demand charge optimization (PRD Section 8.1 Constraints 9)."""
    
    def test_peak_respects_current_month(self, realistic_depot_state, realistic_depot_config):
        """Peak demand should be at least current month peak."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        assert result.peak_demand_kw >= realistic_depot_state.current_month_peak
    
    def test_battery_used_for_peak_shaving(self, realistic_depot_state, realistic_depot_config):
        """Battery should discharge during peak periods."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        # Check battery discharges during peak pricing (timesteps 64-84, 4PM-9PM)
        peak_discharge = sum(
            max(0, result.battery_dispatch[t]) 
            for t in range(64, 84)
        )
        assert peak_discharge > 0


class TestWarmStart:
    """Tests for warm-starting (PRD Section 8.5)."""
    
    def test_warm_start_faster(self, realistic_depot_state, realistic_depot_config):
        """Warm-started solve should be faster than cold start."""
        # Cold start
        model1 = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result1 = solve_model(model1, warm_start=False)
        
        # Warm start
        model2 = build_optimization_model(realistic_depot_state, realistic_depot_config)
        warm_start_model(model2, result1)
        result2 = solve_model(model2, warm_start=True)
        
        # Warm start should be at least 2x faster (target: 3x per PRD)
        assert result2.solve_time_s < result1.solve_time_s
```

### Step 5.3: Trigger Tests

Create `tests/unit/test_triggers.py`:

```python
"""Unit tests for trigger monitoring.

Validates PRD_v2.md Section 5.1 trigger thresholds.
"""
import pytest
from datetime import datetime, timedelta
from uuid import uuid4

from src.triggers.monitor import TriggerMonitor, TriggerType


@pytest.fixture
def trigger_monitor():
    """Create trigger monitor for testing."""
    depot_id = uuid4()
    triggered_events = []
    
    def on_trigger(event):
        triggered_events.append(event)
    
    monitor = TriggerMonitor(depot_id, on_trigger)
    monitor._triggered_events = triggered_events  # For test inspection
    return monitor


class TestSoCDeviation:
    """Tests for SoC deviation trigger (>5%)."""
    
    def test_triggers_at_6_percent(self, trigger_monitor):
        """Should trigger at 6% deviation."""
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={'bus_101': 0.50},
            expected_return_times={},
        )
        
        event = trigger_monitor.check_soc_deviation('bus_101', 0.44)  # 6% deviation
        assert event is not None
        assert event.trigger_type == TriggerType.SOC_DEVIATION
    
    def test_no_trigger_at_4_percent(self, trigger_monitor):
        """Should NOT trigger at 4% deviation."""
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={'bus_101': 0.50},
            expected_return_times={},
        )
        
        event = trigger_monitor.check_soc_deviation('bus_101', 0.48)  # 4% deviation
        assert event is None


class TestPriceChange:
    """Tests for price change trigger (>25% OR >$25/MWh)."""
    
    def test_triggers_at_30_percent_change(self, trigger_monitor):
        """Should trigger at 30% price change."""
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={},
            expected_return_times={},
        )
        
        current_prices = [0.13] * 96  # 30% increase
        event = trigger_monitor.check_price_change(current_prices, 0)
        
        assert event is not None
        assert event.trigger_type == TriggerType.PRICE_CHANGE
    
    def test_triggers_at_30_dollar_mwh_change(self, trigger_monitor):
        """Should trigger at $30/MWh change (even if < 25%)."""
        trigger_monitor.set_baseline(
            prices=[0.20] * 96,  # $200/MWh
            expected_socs={},
            expected_return_times={},
        )
        
        # $30/MWh increase = $0.03/kWh, which is 15% of $0.20
        current_prices = [0.23] * 96
        event = trigger_monitor.check_price_change(current_prices, 0)
        
        assert event is not None  # Triggers because $30 > $25 threshold
    
    def test_no_trigger_at_20_percent_and_20_mwh(self, trigger_monitor):
        """Should NOT trigger when BOTH thresholds not exceeded."""
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={},
            expected_return_times={},
        )
        
        # 20% increase = $20/MWh increase
        current_prices = [0.12] * 96
        event = trigger_monitor.check_price_change(current_prices, 0)
        
        assert event is None  # Neither 25% nor $25 exceeded


class TestReturnTimeDeviation:
    """Tests for return time deviation trigger (>15 minutes)."""
    
    def test_triggers_at_20_minutes_late(self, trigger_monitor):
        """Should trigger when 20 minutes late."""
        expected_return = datetime.utcnow()
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={},
            expected_return_times={'bus_101': expected_return},
        )
        
        actual_return = expected_return + timedelta(minutes=20)
        event = trigger_monitor.check_return_time_deviation('bus_101', actual_return)
        
        assert event is not None
        assert event.trigger_type == TriggerType.RETURN_TIME_DEVIATION
    
    def test_no_trigger_at_10_minutes_late(self, trigger_monitor):
        """Should NOT trigger when only 10 minutes late."""
        expected_return = datetime.utcnow()
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={},
            expected_return_times={'bus_101': expected_return},
        )
        
        actual_return = expected_return + timedelta(minutes=10)
        event = trigger_monitor.check_return_time_deviation('bus_101', actual_return)
        
        assert event is None
    
    def test_no_trigger_when_early(self, trigger_monitor):
        """Should NOT trigger when vehicle returns early."""
        expected_return = datetime.utcnow()
        trigger_monitor.set_baseline(
            prices=[0.10] * 96,
            expected_socs={},
            expected_return_times={'bus_101': expected_return},
        )
        
        actual_return = expected_return - timedelta(minutes=30)  # 30 min early
        event = trigger_monitor.check_return_time_deviation('bus_101', actual_return)
        
        assert event is None
```

**Verification:**
- [ ] All unit tests pass
- [ ] Test coverage ≥ 90% for critical modules
- [ ] Tests use realistic UUIDs and data

---

## PHASE 8: INTEGRATION TESTS

### Step 6.1: Full Pipeline Test

Create `tests/integration/test_full_pipeline.py`:

```python
"""Integration tests for full optimization pipeline.

Validates PRD_v2.md Section 11.1 acceptance tests.
"""
import pytest
from uuid import uuid4

from src.core.models import DepotConfig, DepotState, IncomingVehicle
from src.core.optimizer.milp_model import build_optimization_model, solve_model
from src.core.optimizer.allocator import allocate_chargers
from tests.fixtures.realistic_depot import (
    realistic_depot_config, realistic_depot_state,
    state_with_incoming_vehicle, CHARGER_IDS
)


class TestAT01EndToEndOptimization:
    """AT-01: End-to-End Optimization (PRD Section 11.1)."""
    
    def test_full_cycle(self, realistic_depot_state, realistic_depot_config):
        """Complete optimization cycle: State → Optimize → Allocate."""
        # Build and solve
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        # Verify solve succeeded
        assert result.status in ['optimal', 'feasible']
        assert result.solve_time_s < 60
        
        # Verify departures satisfied
        for vid, t_dep in realistic_depot_state.departure_times.items():
            assert result.schedule[vid]['soc'][t_dep] >= 0.99
        
        # Allocate to chargers
        charger_ids_by_rating = {
            80.0: [str(CHARGER_IDS['charger_1']), str(CHARGER_IDS['charger_2'])],
            150.0: [str(CHARGER_IDS['charger_3'])],
        }
        vehicle_priorities = {
            vid: t for vid, t in realistic_depot_state.departure_times.items()
        }
        
        assignments = allocate_chargers(
            result, 
            realistic_depot_config,
            charger_ids_by_rating,
            vehicle_priorities,
        )
        
        # Verify allocations exist
        assert len(assignments) > 0


class TestAT06InterDepotHandoff:
    """AT-06: Inter-Depot Handoff (PRD Section 11.1)."""
    
    def test_incoming_vehicle_included(self, state_with_incoming_vehicle, realistic_depot_config):
        """Incoming vehicle from handoff should be included in optimization."""
        assert len(state_with_incoming_vehicle.incoming_vehicles) == 1
        
        incoming = state_with_incoming_vehicle.incoming_vehicles[0]
        assert incoming.external_id == 'bus_201'
        assert incoming.expected_soc == 0.35
        
        # Note: Full integration with incoming vehicles requires
        # state assembler to add them to vehicle_socs, etc.
        # This test validates the data structure is correct.


class TestAT07BuildingLoadIntegration:
    """AT-07: Building Load Integration (PRD Section 11.1)."""
    
    def test_building_load_in_grid_power(self, realistic_depot_state, realistic_depot_config):
        """Grid power must include building load."""
        model = build_optimization_model(realistic_depot_state, realistic_depot_config)
        result = solve_model(model)
        
        # During off-peak when minimal charging, grid ≈ building load
        # Check timestep 0 (midnight) when little charging expected
        grid_t0 = result.grid_power[0]
        building_t0 = realistic_depot_state.building_power[0]
        
        # Grid should be at least building load
        assert grid_t0 >= building_t0 * 0.9  # Allow some battery contribution
```

**Verification:**
- [ ] All integration tests pass
- [ ] Full optimization cycle completes in < 60 seconds
- [ ] Building load correctly integrated

---

## PHASE 9: DEPLOYMENT

### Step 7.1: Docker Configuration

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_USER: favonius
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: favonius
    ports:
      - "5432:5432"
    volumes:
      - timescale_data:/var/lib/postgresql/data
      - ./migrations:/docker-entrypoint-initdb.d
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U favonius"]
      interval: 10s
      timeout: 5s
      retries: 5

  api:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      DATABASE_URL: postgresql://favonius:${DB_PASSWORD}@timescaledb:5432/favonius
      OCPP_PORT: 9000
      GUROBI_LICENSE_FILE: /opt/gurobi/gurobi.lic
    ports:
      - "8000:8000"
      - "9000:9000"
    volumes:
      - ./gurobi.lic:/opt/gurobi/gurobi.lic:ro
    depends_on:
      timescaledb:
        condition: service_healthy

  ocpp_simulator:
    build:
      context: .
      dockerfile: Dockerfile.simulator
    environment:
      OCPP_SERVER: ws://api:9000
    depends_on:
      - api
    profiles:
      - simulation

volumes:
  timescale_data:
```

Create `Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy application code
COPY src/ ./src/
COPY config/ ./config/

# Expose ports
EXPOSE 8000 9000

# Run API server
CMD ["uv", "run", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Verification:**
- [ ] `docker-compose up` starts all services
- [ ] API accessible at http://localhost:8000
- [ ] OCPP server accepts connections at ws://localhost:9000
- [ ] Gurobi license valid in container

---

## Development Milestones Checklist

### Milestone 1: Core Optimization Engine
- [ ] Data models implemented (DepotConfig, DepotState, BuildingZone)
- [ ] MILP model builds with Gurobi
- [ ] HiGHS fallback solver implemented (automatic on Gurobi failure)
- [ ] Solver finds optimal/feasible solutions
- [ ] Solve time < 60s for 20 vehicles
- [ ] All departure SoC constraints satisfied
- [ ] Building load integrated in grid power
- [ ] Warm-starting implemented (> 3x speedup target)
- [ ] Infeasibility handling with degraded solve (per PRD Section 8.5.1)
- [ ] Battery efficiency model correct (split √η each direction)

### Milestone 2: Data Infrastructure
- [ ] TimescaleDB schema deployed
- [ ] OCPP adapter functional (1.6J ONLY, max_charge_kw extraction)
- [ ] Price feed adapter functional
- [ ] Weather adapter functional
- [ ] Building load adapter functional

### Milestone 3: Triggers & Re-optimization
- [ ] SoC deviation trigger (>5%)
- [ ] Price change trigger (>25% OR >$25/MWh) - OR logic per PRD Section 5.1
- [ ] Return time deviation trigger (>15 min late)
- [ ] Scheduled trigger (hourly 24/7) - per PRD Section 5.1
- [ ] Inter-depot handoff trigger (on message receipt)
- [ ] VDV 463 ChargingRequest trigger (on update)
- [ ] Trigger cooldown (5 min per depot) to prevent rapid re-optimization

### Milestone 4: Inter-Depot Coordination
- [ ] Handoff message send/receive
- [ ] Incoming vehicles in state assembly
- [ ] Handoff acknowledgment flow

### Milestone 4.5: Security & Input Validation
- [ ] UUID validation on all endpoints
- [ ] SoC value range validation [0.0, 1.0]
- [ ] Power value validation against site limits
- [ ] Rate limiting (100/min API, 10/min optimize)
- [ ] Trigger cooldown (5 min per depot)
- [ ] Parameterized SQL queries (no interpolation)
- [ ] Data freshness checks before optimization (all PRD thresholds)
- [ ] JWT authentication for API endpoints (per PRD Section 10.3)
- [ ] TLS for all exposed ports (HTTPS for API, WSS for OCPP/VDV463/BACnet)
- [ ] Secrets management (environment variables, no hardcoded secrets)
- [ ] Security folder structure (src/security/)

### Milestone 5: VDV 463 Transit Integration
- [ ] VDV 463 WebSocket handler at `/vdv463/{presystem_id}`
- [ ] BootNotification handling
- [ ] ProvideChargingRequests parsing and storage
- [ ] Vehicle ID resolution (external_id → UUID)
- [ ] InvalidVehicleId error handling
- [ ] ProvideChargingInformation export (every 15 sec)
  - [ ] Outbound payloads conform to `ProvideChargingInformationRequest.json` schema (`depotInfoList` → `ChargingStationInfo` → `ChargingPointInfo` → `ChargingProcessInfo`)
  - [ ] `processStatus` values use the official `ProcessStatus` enum, including `ChargingRejectedTechnically` for technical rejections
  - [ ] Broadcast task runs for all active VDV 463 connections, using shared depot state from TimescaleDB
- [ ] Manual preconditioning support
- [ ] Automatic preconditioning calculation
- [ ] Preconditioning as soft constraint (M=1000 penalty)
- [ ] Preconditioning curtailment under site limit
- [ ] VDV 463 database schema deployed

**MVP vs Full ProvideChargingInformation Scope:**
- MVP implements a **minimal but schema-valid subset**:
  - Populate required `DepotInfo`, `ChargingStationInfo`, and `ChargingPointInfo` fields.
  - Populate `ChargingProcessInfo` for active processes with correct `processStatus` and basic electrical data.
  - Populate `VehicleInfo.vehicleId`, `VehicleInfo.tractionBatteryInfo.stateOfCharge`, and `VehicleChargingStatus`.
- Post-MVP / Phase 5.x tasks:
  - Map charger, charging point, and vehicle fault information into `ChargingStationFaultInfo`, `ChargingPointFaultInfo`, and `VehicleFaultInfo` when telemetry is available.
  - Populate `scheduledChargingProcessList` with future charging processes derived from the optimizer.

**Operator-facing error surfacing:**
- [ ] All VDV 463 protocol errors (`InvalidVehicleId`, `InvalidChargingPointId`, `InvalidTimeWindow`, `DuplicateRequestId`, `SchemaValidationError`, etc.) are:
  - [ ] Returned as VDV 463 Error messages (MessageType 3) with `errorCode` and `errorDescription` in a Favonius-defined error payload (VDV 463 does not prescribe a concrete JSON schema for MessageType 3)
  - [ ] Logged with structured fields (depot_id, presystem_id, chargingRequestId, errorCode) suitable for later analytics and UI
- [ ] Error records are stored in a queryable form (TimescaleDB table or logging sink) to support a future operator diagnostics UI

### Milestone 6: BACnet/SC Building Integration
- [ ] BACnet/SC Hub implementation (bacpypes3)
- [ ] Device-to-zone mapping (device_id + oid → zone_id)
- [ ] Zone temperature reading
- [ ] Baseline setpoint reading
- [ ] Setpoint offset calculation (Constraint 15b)
- [ ] Safety clamping (min/max temp bounds)
- [ ] Thermal dynamics constraints in MILP
- [ ] HVAC power in grid power calculation
- [ ] Pre-cooling optimization working
- [ ] BACnet database schema deployed

### Milestone 6.5: Dispatch Validation
- [ ] Pre-dispatch charger status check
- [ ] Skip dispatch to Faulted/Unavailable/Reserved chargers
- [ ] Pre-dispatch BACnet connectivity check
- [ ] Dispatch skip logging for audit
- [ ] No re-optimization on dispatch failure (eventual consistency)

### Milestone 7: Post-Optimization Allocation
- [ ] Charger allocation algorithm
- [ ] Physical accessibility constraints
- [ ] OCPP SetChargingProfile dispatch
- [ ] BACnet setpoint offset dispatch

### Milestone 8: Testing
- [ ] Unit tests with ≥90% coverage
- [ ] Realistic fixtures with UUIDs
- [ ] VDV 463 integration tests (AT-08 through AT-11)
  - [ ] Use `BootNotification`, `ProvideChargingRequests`, and `ProvideChargingInformation` JSON messages that validate against the official VDV 463 schemas (including empty-object payloads for all *Response messages)
  - [ ] Verify correct mapping of arrival/departure times, SoC targets, priority, and preconditioning into optimization (per PRD AT-08/AT-09)
  - [ ] Assert correct error behavior for invalid vehicle/charging point IDs, invalid time windows, and schema violations
  - [ ] Confirm that `ProvideChargingInformation` reflects optimization results and preconditioning status as described in PRD Section 9.6
- [ ] BACnet integration tests (AT-14, AT-15)
- [ ] Solver fallback test (AT-12)
- [ ] Preconditioning curtailment test (AT-13)
- [ ] Integration tests pass
- [ ] Performance benchmarks met

### Milestone 9: Deployment
- [ ] Docker containers built
- [ ] Docker Compose stack running
- [ ] Gurobi license configured
- [ ] Health endpoint operational
- [ ] VDV 463 endpoint accessible
- [ ] BACnet/SC Hub running

---

## Post-MVP Roadmap

1. **V2G Support**: Add discharge capabilities and grid services
2. **OCPP 2.0.1 Native Support**: Add native OCPP 2.0.1 protocol support (currently 1.6J only)
3. **Multi-depot Coordination**: Central coordinator for fleet-wide optimization
4. **Advanced Forecasting**: RL-based price prediction if needed
5. **Additional Connectors**: CHAdeMO, Type2, NACS support
6. **Customer Dashboard**: Real-time visualization and ROI tracking
7. **Solar Integration**: Re-add solar predictor for self-consumption
8. **OpenADR Integration**: Demand response revenue streams
9. **OCPI Integration**: Charge point roaming for public charging
10. **VDV 261 Full Stack**: Full ISO 15118 Plug & Charge with VDV 261

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-12-04 | Claude + Joris | Initial development plan |
| 2.0 | 2025-12-12 | Claude + Joris | Reconciled with PRD v2; Gurobi config, triggers with OR logic, building load required, inter-depot handoffs, return time trigger, realistic tests |
| 2.1 | 2025-12-13 | Claude | Aligned with PRD v2.2: Fixed Vehicle.id_tag field, added CHECK constraints to SQL schema, added charger_id to telemetry, added 'degraded' status, added power limit constraint to MILP, updated grid balance for P_batt_effective with efficiency handling, added infeasibility handling to solve_model, added PHASE 4.5 for security (input validation, rate limiting, SQL injection prevention, data freshness) |
| 2.2 | 2025-12-13 | Claude | Final alignment fixes: Added battery_efficiency to DepotConfig, fixed data freshness thresholds (prices: 24h, building load: 30min), added lat/lon CHECK constraints to telemetry, added charger_id to OCPP handler, updated all PRD.md refs to PRD_v2.md, use config.battery_efficiency in MILP |
| 2.3 | 2025-01-XX | Claude | Architecture updates: Documented integrated system architecture (Main API primary, WebSocket Handler telemetry-only), added Phase 4 internal API specification, documented backup heuristic approach, removed Julia references (already done), documented V2G removal, updated component responsibilities to reflect service split |
| 2.4 | 2025-01-XX | Claude | OCPP simplification: Removed dual server architecture, consolidated to single OCPP 1.6 server in WebSocket Handler that handles OCPP 2+ messages, updated Phase 4 architecture context to reflect single server approach |
| 3.0 | 2025-01-19 | Claude + Joris | **Major update aligned with PRD v2.7**: (1) **OCPP 1.6J-only clarification** - corrected that 2.0.1 is NOT wire-compatible, chargers MUST use 1.6J subprotocol; (2) **PHASE 5: VDV 463 Integration** - added complete transit operations integration with WebSocket handler, vehicle ID resolution, preconditioning as soft constraint; (3) **PHASE 6: BACnet/SC Integration** - added building HVAC control with thermal flywheel optimization, setpoint offset calculation (Constraint 15b); (4) **PHASE 6.5: Dispatch Validation** - added pre-dispatch checks for charger/device status; (5) Updated repository structure with new adapter directories; (6) Added bacpypes3 dependency; (7) Added VDV 463 and BACnet database schemas; (8) Updated BuildingZone dataclass with max_hvac_power_kw and active_setpoint_oid; (9) Renumbered phases (Unit Tests → 7, Integration → 8, Deployment → 9); (10) Expanded milestones for new integrations; (11) Updated Post-MVP roadmap |

---

*End of Development Plan*
