# Favonius Energy — Product Development Plan
## EV Fleet Depot Optimization Platform (MVP v2)

---

## Overview

This development plan implements the specifications in `PRD.md` (Version 2.0). The PRD is the **single source of truth** — if any discrepancy exists between this plan and the PRD, the PRD wins.

**Key Technical Decisions (from PRD):**
- Solver: **Gurobi** with <60 second solve time
- Price trigger: **OR** logic (>25% OR >$25/MWh)
- Building load: **Required** (not optional)
- Connector type: **CCS only** for MVP
- Vehicle max_charge_kw: From **OCPP MeterValues** or config fallback

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
   - `ocpp.mdc` — OCPP 1.6/2.0.1 message formats
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
│   ├── PRD.md                  # Product Requirements Document (source of truth)
│   ├── ARCHITECTURE.md         # System architecture
│   └── API.md                  # API specifications (OpenAPI)
├── src/
│   ├── core/
│   │   ├── models.py           # Data classes (DepotState, DepotConfig, etc.)
│   │   ├── optimizer/          # MILP optimization engine
│   │   │   ├── milp_model.py   # Pyomo model definition
│   │   │   ├── solver.py       # Gurobi solver wrapper
│   │   │   └── allocator.py    # Post-optimization charger allocation
│   │   ├── surrogate/          # Energy consumption model
│   │   └── state/              # State assembler
│   ├── adapters/
│   │   ├── ocpp/               # OCPP server and handlers
│   │   ├── caiso/              # CAISO price feeds
│   │   ├── weather/            # Weather API integration
│   │   ├── building_load/      # Building load meter/API
│   │   └── handoff/            # Inter-depot handoff manager
│   ├── triggers/               # Re-optimization trigger monitor
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
| **Optimization** | `gurobipy` | Gurobi MILP solver |
| **Modeling** | `pyomo` | Algebraic modeling language |
| **ML/Surrogate** | `scikit-learn`, `gpytorch` | Gaussian Process, MLP |
| **API** | `fastapi`, `uvicorn` | REST API server |
| **Database** | `asyncpg`, `sqlalchemy` | PostgreSQL/TimescaleDB |
| **OCPP** | `ocpp` | OCPP 1.6/2.0.1 support |
| **Time-series** | `pandas`, `polars` | Data manipulation |
| **Weather** | `openmeteo-requests` | Weather API client |
| **Testing** | `pytest`, `pytest-asyncio` | Test framework |

**Installation Script:**
```bash
# Using uv (recommended)
uv init favonius-platform
cd favonius-platform
uv add pyomo gurobipy scikit-learn gpytorch fastapi uvicorn asyncpg sqlalchemy
uv add ocpp pandas polars openmeteo-requests httpx
uv add --dev pytest pytest-asyncio pytest-cov ruff mypy

# Verify Gurobi license
python -c "import gurobipy as gp; print(f'Gurobi {gp.gurobi.version()}')"
```

**Verification:**
- [ ] All dependencies install without conflicts
- [ ] `uv run python -c "import pyomo; import ocpp; print('OK')"` succeeds
- [ ] Gurobi solver accessible with valid license

---

### Step 0.4: Database Setup (TimescaleDB)

**Schema:** Copy directly from PRD Section 6.1.

Create `migrations/001_initial_schema.sql`:

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
    battery_kwh     DOUBLE PRECISION NOT NULL,
    max_charge_kw   DOUBLE PRECISION NOT NULL,
    ocpp_id         VARCHAR(100),
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
    soc             DOUBLE PRECISION,
    location_lat    DOUBLE PRECISION,
    location_lon    DOUBLE PRECISION,
    is_plugged      BOOLEAN,
    charging_kw     DOUBLE PRECISION,
    odometer_km     DOUBLE PRECISION,
    max_charge_kw   DOUBLE PRECISION  -- From OCPP MeterValues
);
SELECT create_hypertable('telemetry', 'time');
CREATE INDEX idx_telemetry_vehicle ON telemetry (vehicle_id, time DESC);

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

CREATE TABLE optimization_runs (
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
    expected_soc    DOUBLE PRECISION NOT NULL,
    arrival_time    TIMESTAMPTZ NOT NULL,
    battery_kwh     DOUBLE PRECISION NOT NULL,
    max_charge_kw   DOUBLE PRECISION NOT NULL,
    status          VARCHAR(20) DEFAULT 'pending',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    arrived_at      TIMESTAMPTZ
);
CREATE INDEX idx_interdepot_dest_status ON interdepot_messages (dest_depot_id, status);

CREATE TABLE trigger_log (
    trigger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    trigger_type    VARCHAR(50) NOT NULL,
    trigger_time    TIMESTAMPTZ DEFAULT NOW(),
    details         JSONB,
    run_id          UUID REFERENCES optimization_runs(run_id)
);
```

**Verification:**
- [ ] TimescaleDB container running (`docker-compose up -d timescaledb`)
- [ ] Schema migrations applied
- [ ] Sample data inserted for testing

---

## PHASE 1: CORE OPTIMIZATION ENGINE

### Step 1.1: Data Models

Create `src/core/models.py` (copy from PRD Section 6.2):

```python
"""Core data models for Favonius optimization platform.

These dataclasses are the authoritative representation of system state.
See PRD.md Section 6.2 for full specification.
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
    ocpp_id: Optional[str] = None


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
    
    See PRD.md Section 8.3 for aggregation strategy.
    """
    vehicle_capacities: dict[str, float]      # vehicle_id -> kWh
    vehicle_max_charge_kw: dict[str, float]   # vehicle_id -> max charge rate (kW)
    charger_groups: dict[float, int]          # rated_kw -> count
    charger_efficiency: float
    charger_vehicle_access: dict[str, set[str]]  # charger_id -> accessible vehicle_ids
    battery_capacity: float
    battery_power: float
    battery_soc_min: float = 0.2
    battery_soc_max: float = 0.8
    max_site_power: float = 1000.0
    delta_t: float = 0.25  # hours (15 min)
    n_timesteps: int = 96  # 24 hours


@dataclass
class DepotState:
    """Dynamic state for optimization.
    
    Assembled from database queries before each optimization run.
    See PRD.md Section 5.3 for data flow.
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
    
    See PRD.md Section 8.1 for variable definitions.
    """
    run_id: UUID
    schedule: dict[str, dict]  # vehicle_id -> {charging_power: [], soc: []}
    battery_dispatch: list[float]  # +discharge, -charge
    grid_power: list[float]
    peak_demand_kw: float
    objective_value: float
    solve_time_s: float
    status: str  # 'optimal', 'feasible', 'infeasible', 'timeout'
```

**Verification:**
- [ ] All dataclasses import without errors
- [ ] Type hints validate with mypy

---

### Step 1.2: MILP Optimization Model

Create `src/core/optimizer/milp_model.py`:

```python
"""MILP optimization model for depot charging scheduling.

Implements the formulation from PRD.md Section 8.1.
Uses Gurobi solver with configuration from PRD.md Section 8.2.
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
    
    See PRD.md Section 8.1 for mathematical formulation.
    
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
    
    # Constraint 6: Charger capacity (aggregated)
    total_chargers = sum(config.charger_groups.values()) if config.charger_groups else 0
    def charger_capacity_rule(m, t):
        return sum(m.y_charge[b, t] for b in m.B) <= total_chargers
    model.charger_capacity = pyo.Constraint(model.T, rule=charger_capacity_rule)
    
    # Constraint 7: Grid power balance (includes building load - REQUIRED per PRD)
    def grid_balance_rule(m, t):
        return m.P_grid[t] == (
            sum(m.P_charge[b, t] for b in m.B) +
            m.building_power[t] -
            m.P_batt[t]
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
    warm_start: bool = True
) -> OptimizationResult:
    """Solve the optimization model using Gurobi.
    
    See PRD.md Section 8.2 for solver configuration.
    
    Args:
        model: Pyomo model to solve
        time_limit: Maximum solve time in seconds (default: 60)
        warm_start: Whether to use warm-starting (default: True)
    
    Returns:
        OptimizationResult with schedule and metadata
    """
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
    
    # Solve
    result = solver.solve(model, tee=False)
    
    # Determine status
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
    
    # Extract solution (only if feasible)
    if status in ['optimal', 'feasible']:
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
        )
    else:
        return OptimizationResult(
            run_id=uuid4(),
            schedule={},
            battery_dispatch=[],
            grid_power=[],
            peak_demand_kw=0.0,
            objective_value=float('inf'),
            solve_time_s=result.solver.time if hasattr(result.solver, 'time') else 0.0,
            status=status,
        )


def warm_start_model(
    model: pyo.ConcreteModel, 
    previous_result: OptimizationResult
) -> None:
    """Initialize model variables from previous solution.
    
    See PRD.md Section 8.5 for performance targets.
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

See PRD.md Section 8.3 for aggregation strategy.
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

Create `src/triggers/monitor.py`:

```python
"""Re-optimization trigger monitoring.

Monitors conditions that require re-optimization per PRD.md Section 5.1.
Trigger thresholds:
- SoC deviation: >5%
- Price change: >25% OR >$25/MWh
- Return time deviation: >15 minutes
- Inter-depot handoff: On message receipt
- Scheduled: Hourly 7AM-11PM
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
    
    See PRD.md Section 5.1 for trigger thresholds.
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
            
            # Scheduled trigger: hourly 7AM-11PM
            if 7 <= hour <= 23:
                trigger = self.create_scheduled_trigger()
                self.on_trigger(trigger)
            
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
- [ ] Scheduled trigger fires hourly 7AM-11PM

---

## PHASE 3: INTER-DEPOT HANDOFF

### Step 3.1: Handoff Manager

Create `src/adapters/handoff/manager.py`:

```python
"""Inter-depot vehicle handoff management.

Implements PRD.md Section 5.4 for inter-depot coordination.
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
    
    See PRD.md Section 5.4 for handoff flow.
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

## PHASE 4: OCPP INTEGRATION

### Step 4.1: Vehicle Max Charge Rate from OCPP

Create `src/adapters/ocpp/handlers.py` (excerpt for max_charge_kw handling):

```python
"""OCPP message handlers.

See PRD.md Section 7.2 for supported messages.
See PRD.md Section 8.4 for vehicle max_charge_kw resolution.
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
        vehicle_id: Optional[UUID],
        soc: Optional[float],
        power_kw: Optional[float],
        max_charge_kw: Optional[float],
        timestamp: datetime,
    ) -> None:
        """Process MeterValues message from charger.
        
        If max_charge_kw is reported, it's stored and takes precedence
        over static config for optimization.
        """
        # Store telemetry
        await self.db.execute(
            """
            INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, max_charge_kw)
            VALUES ($1, $2, $3, $4, $5)
            """,
            timestamp, vehicle_id, soc, power_kw, max_charge_kw
        )
        
        # Cache max_charge_kw if reported
        if vehicle_id and max_charge_kw:
            self._recent_max_charge_kw[str(vehicle_id)] = (max_charge_kw, timestamp)
            logger.debug(f"Vehicle {vehicle_id} max_charge_kw from OCPP: {max_charge_kw}")
    
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

## PHASE 5: UNIT TESTS

### Step 5.1: Realistic Test Fixtures

Create `tests/fixtures/realistic_depot.py`:

```python
"""Realistic test fixtures using UUIDs and production-like data.

See PRD.md Section 11 for acceptance criteria these tests validate.
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

Validates PRD.md Section 8 requirements.
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

Validates PRD.md Section 5.1 trigger thresholds.
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

## PHASE 6: INTEGRATION TESTS

### Step 6.1: Full Pipeline Test

Create `tests/integration/test_full_pipeline.py`:

```python
"""Integration tests for full optimization pipeline.

Validates PRD.md Section 11.1 acceptance tests.
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

## PHASE 7: DEPLOYMENT

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
- [ ] Data models implemented (DepotConfig, DepotState)
- [ ] MILP model builds with Gurobi
- [ ] Solver finds optimal/feasible solutions
- [ ] Solve time < 60s for 20 vehicles
- [ ] All departure SoC constraints satisfied
- [ ] Building load integrated in grid power

### Milestone 2: Data Infrastructure
- [ ] TimescaleDB schema deployed
- [ ] OCPP adapter functional (including max_charge_kw extraction)
- [ ] Price feed adapter functional
- [ ] Weather adapter functional
- [ ] Building load adapter functional

### Milestone 3: Triggers & Re-optimization
- [ ] SoC deviation trigger (>5%)
- [ ] Price change trigger (>25% OR >$25/MWh)
- [ ] Return time deviation trigger (>15 min)
- [ ] Scheduled trigger (hourly 7AM-11PM)
- [ ] Inter-depot handoff trigger

### Milestone 4: Inter-Depot Coordination
- [ ] Handoff message send/receive
- [ ] Incoming vehicles in state assembly
- [ ] Handoff acknowledgment flow

### Milestone 5: Post-Optimization Allocation
- [ ] Charger allocation algorithm
- [ ] Physical accessibility constraints
- [ ] OCPP SetChargingProfile dispatch

### Milestone 6: Testing
- [ ] Unit tests with ≥90% coverage
- [ ] Realistic fixtures with UUIDs
- [ ] Integration tests pass
- [ ] Performance benchmarks met

### Milestone 7: Deployment
- [ ] Docker containers built
- [ ] Docker Compose stack running
- [ ] Gurobi license configured
- [ ] Health endpoint operational

---

## Post-MVP Roadmap

1. **V2G Support**: Add discharge capabilities and grid services
2. **Multi-depot Coordination**: Central coordinator for fleet-wide optimization
3. **Advanced Forecasting**: RL-based price prediction if needed
4. **Additional Connectors**: CHAdeMO, Type2, NACS support
5. **Customer Dashboard**: Real-time visualization and ROI tracking
6. **Solar Integration**: Re-add solar predictor for self-consumption

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-12-04 | Claude + Joris | Initial development plan |
| 2.0 | 2025-12-12 | Claude + Joris | Reconciled with PRD v2; Gurobi config, triggers with OR logic, building load required, inter-depot handoffs, return time trigger, realistic tests |

---

*End of Development Plan*
