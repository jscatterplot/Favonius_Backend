# Favonius Energy — Product Development Plan
## EV Fleet Depot Optimization Platform (MVP v1)

---

## PHASE 0: FOUNDATION & PROJECT SETUP

### Step 0.1: Development Environment Configuration

**Objective:** Establish a reproducible, AI-assisted development environment.

**Actions:**
1. Install Cursor IDE (or VS Code with GitHub Copilot as fallback)
2. Create `.cursorrules` file in project root with persona: "You are a senior Python/Julia developer specializing in energy systems optimization and OCPP protocols"
3. Set up Git repository with conventional commits
4. Create `.cursor/rules/` directory with domain-specific rules:
   - `optimization.mdc` — MILP formulation patterns
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
│   ├── PRD.md                  # Product Requirements Document
│   ├── ARCHITECTURE.md         # System architecture
│   └── API.md                  # API specifications
├── src/
│   ├── core/
│   │   ├── optimizer/          # MILP optimization engine
│   │   ├── surrogate/          # Energy consumption model
│   │   └── state/              # State assembler
│   ├── adapters/
│   │   ├── ocpp/               # OCPP client/server
│   │   ├── caiso/              # CAISO price feeds
│   │   └── weather/            # Weather API integration
│   ├── api/                    # FastAPI REST endpoints
│   └── db/                     # Database models & migrations
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/               # Test data (routes, SoC, prices)
├── scripts/
│   └── simulation/             # Simulation harnesses
├── config/
│   ├── depot_config.yaml       # Depot-specific parameters
│   └── tariff_config.yaml      # Utility rate structures
├── pyproject.toml
├── docker-compose.yml
└── Makefile
```

**Verification:**
- [ ] All directories created
- [ ] `pyproject.toml` with Poetry/uv configuration
- [ ] `docker-compose.yml` with TimescaleDB, Redis (optional), and app services

---

### Step 0.3: Technology Stack Installation

**Core Dependencies:**

| Component | Package | Purpose |
|-----------|---------|---------|
| **Optimization** | `highspy` or `gurobipy` | MILP solver |
| **Modeling** | `pyomo` | Algebraic modeling language |
| **ML/Surrogate** | `scikit-learn`, `gpytorch` | Gaussian Process, MLP |
| **API** | `fastapi`, `uvicorn` | REST API server |
| **Database** | `asyncpg`, `sqlalchemy` | PostgreSQL/TimescaleDB |
| **OCPP** | `ocpp` (mobilityhouse) | OCPP 1.6/2.0.1 support |
| **Time-series** | `pandas`, `polars` | Data manipulation |
| **Weather** | `openmeteo-requests` | Weather API client |
| **Testing** | `pytest`, `pytest-asyncio` | Test framework |

**Installation Script:**
```bash
# Using uv (recommended)
uv init favonius-platform
cd favonius-platform
uv add pyomo highspy scikit-learn gpytorch fastapi uvicorn asyncpg sqlalchemy
uv add ocpp pandas polars openmeteo-requests httpx
uv add --dev pytest pytest-asyncio pytest-cov ruff mypy

# Alternatively with Poetry
poetry init
poetry add pyomo highspy scikit-learn gpytorch fastapi uvicorn asyncpg sqlalchemy
poetry add ocpp pandas polars openmeteo-requests httpx
poetry add --group dev pytest pytest-asyncio pytest-cov ruff mypy
```

**Verification:**
- [ ] All dependencies install without conflicts
- [ ] `uv run python -c "import pyomo; import ocpp; print('OK')"` succeeds
- [ ] HiGHS solver accessible: `highspy.Highs().run()` returns

---

### Step 0.4: Database Setup (TimescaleDB)

**Schema Design:**

```sql
-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Depot configuration (reference data)
CREATE TABLE depots (
    depot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    utility_account_id VARCHAR(100),
    max_grid_power_kw DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Vehicles (EV fleet)
CREATE TABLE vehicles (
    vehicle_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID REFERENCES depots(depot_id),
    vehicle_type VARCHAR(50),  -- 'bus_large', 'bus_small', 'van', 'truck'
    battery_capacity_kwh DOUBLE PRECISION NOT NULL,
    max_charge_rate_kw DOUBLE PRECISION NOT NULL,
    ocpp_charge_point_id VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Chargers
CREATE TABLE chargers (
    charger_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID REFERENCES depots(depot_id),
    rated_power_kw DOUBLE PRECISION NOT NULL,
    efficiency DOUBLE PRECISION DEFAULT 0.95,
    ocpp_charge_point_id VARCHAR(100) UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Schedules (routes as fixed input)
CREATE TABLE schedules (
    schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(vehicle_id),
    departure_time TIMESTAMPTZ NOT NULL,
    return_time TIMESTAMPTZ NOT NULL,
    route_id VARCHAR(100),
    estimated_energy_kwh DOUBLE PRECISION,
    required_soc_at_departure DOUBLE PRECISION DEFAULT 1.0,
    destination_depot_id UUID REFERENCES depots(depot_id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Telemetry (hypertable for time-series)
CREATE TABLE telemetry (
    time TIMESTAMPTZ NOT NULL,
    vehicle_id UUID NOT NULL,
    soc DOUBLE PRECISION,
    location_lat DOUBLE PRECISION,
    location_lon DOUBLE PRECISION,
    is_plugged_in BOOLEAN,
    charging_power_kw DOUBLE PRECISION
);
SELECT create_hypertable('telemetry', 'time');

-- Optimization results
CREATE TABLE optimization_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID REFERENCES depots(depot_id),
    run_time TIMESTAMPTZ DEFAULT NOW(),
    horizon_start TIMESTAMPTZ,
    horizon_end TIMESTAMPTZ,
    objective_value DOUBLE PRECISION,
    solve_time_seconds DOUBLE PRECISION,
    trigger_reason VARCHAR(100),
    schedule_json JSONB
);

-- Electricity prices
CREATE TABLE prices (
    time TIMESTAMPTZ NOT NULL,
    depot_id UUID NOT NULL,
    price_per_kwh DOUBLE PRECISION,
    demand_charge_per_kw DOUBLE PRECISION,
    price_source VARCHAR(50)  -- 'caiso_dam', 'utility_tou', etc.
);
SELECT create_hypertable('prices', 'time');
```

**Verification:**
- [ ] TimescaleDB container running (`docker-compose up -d timescaledb`)
- [ ] Schema migrations applied
- [ ] Sample data inserted for testing

---

## PHASE 1: CORE OPTIMIZATION ENGINE

### Step 1.1: MILP Problem Formulation

**Objective:** Implement the optimization model from the Stanford CarbonFree paper, simplified for MVP.

**Mathematical Formulation:**

The optimization problem minimizes total electricity cost over a 24-hour horizon:

```
min  Σ_t (price[t] × P_grid[t] × Δt) + Σ_i (demand_charge_rate[i] × P_max_grid[i])
     + penalty × Σ_b,t max(0, SoC_required[b,t] - SoC[b,t])

subject to:
  (1) SoC dynamics:
      SoC[b,t+1] = SoC[b,t] + (η × P_charge[b,t] × Δt) / E_batt[b]  ∀b,t
  
  (2) Vehicle availability:
      P_charge[b,t] = 0  if vehicle b is on route at time t
  
  (3) Departure SoC requirement:
      SoC[b,t_depart] ≥ SoC_required[b]  (HARD CONSTRAINT)
  
  (4) Charger limits:
      P_charge[b,t] ≤ P_max_charger × y_charge[b,t]
      Σ_b y_charge[b,t] ≤ n_chargers
  
  (5) Site power limit:
      P_grid[t] = Σ_b P_charge[b,t] + P_building[t] - P_batt[t]
      P_grid[t] ≤ P_max_site
  
  (6) Battery storage:
      SoC_batt[t+1] = SoC_batt[t] + (P_batt[t] × Δt) / E_batt_storage
      0.2 ≤ SoC_batt[t] ≤ 0.8
  
  (7) Demand charge tracking:
      P_max_grid[i] ≥ P_grid[t]  ∀t ∈ demand_period[i]
```

**Implementation (Pyomo):**

Create `src/core/optimizer/milp_model.py`:

```python
"""MILP optimization model for depot charging scheduling."""
from __future__ import annotations

import pyomo.environ as pyo
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from numpy.typing import NDArray

@dataclass
class DepotState:
    """Current state of the depot for optimization."""
    vehicle_socs: dict[str, float]           # vehicle_id -> SoC [0,1]
    battery_soc: float                        # stationary battery SoC
    prices: list[float]                       # $/kWh for each timestep
    demand_charge_rate: float                 # $/kW
    current_month_peak: float                 # kW (moving limit)
    vehicle_availability: dict[str, list[bool]]  # vehicle_id -> availability per t
    energy_requirements: dict[str, float]    # vehicle_id -> kWh needed
    departure_times: dict[str, int]          # vehicle_id -> timestep index

@dataclass  
class DepotConfig:
    """Static depot configuration."""
    vehicle_capacities: dict[str, float]     # vehicle_id -> kWh
    charger_power: float                      # kW per charger
    charger_efficiency: float                 # 0.90-0.95
    n_chargers: int
    battery_capacity: float                   # kWh
    battery_power: float                      # kW
    max_site_power: float                     # kW
    delta_t: float = 0.25                     # hours (15 min)
    n_timesteps: int = 96                     # 24 hours


def build_optimization_model(
    state: DepotState,
    config: DepotConfig,
) -> pyo.ConcreteModel:
    """Build Pyomo MILP model for depot charging optimization."""
    
    model = pyo.ConcreteModel("DepotCharging")
    
    # Sets
    model.T = pyo.RangeSet(0, config.n_timesteps - 1)
    model.B = pyo.Set(initialize=list(state.vehicle_socs.keys()))
    
    # Parameters
    model.price = pyo.Param(model.T, initialize=lambda m, t: state.prices[t])
    model.E_batt = pyo.Param(model.B, initialize=config.vehicle_capacities)
    model.soc_init = pyo.Param(model.B, initialize=state.vehicle_socs)
    model.available = pyo.Param(
        model.B, model.T, 
        initialize=lambda m, b, t: 1 if state.vehicle_availability[b][t] else 0
    )
    
    # Variables
    model.P_charge = pyo.Var(model.B, model.T, domain=pyo.NonNegativeReals,
                             bounds=(0, config.charger_power))
    model.SoC = pyo.Var(model.B, model.T, bounds=(0.1, 1.0))
    model.y_charge = pyo.Var(model.B, model.T, domain=pyo.Binary)
    model.P_grid = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    model.P_peak = pyo.Var(domain=pyo.NonNegativeReals)
    
    # Battery storage variables
    model.P_batt = pyo.Var(model.T, bounds=(-config.battery_power, config.battery_power))
    model.SoC_batt = pyo.Var(model.T, bounds=(0.2, 0.8))
    
    # Constraints
    
    # SoC dynamics
    def soc_init_rule(m, b):
        return m.SoC[b, 0] == state.vehicle_socs[b]
    model.soc_init_con = pyo.Constraint(model.B, rule=soc_init_rule)
    
    def soc_dynamics_rule(m, b, t):
        if t == 0:
            return pyo.Constraint.Skip
        eta = config.charger_efficiency
        return m.SoC[b, t] == m.SoC[b, t-1] + (
            eta * m.P_charge[b, t-1] * config.delta_t / m.E_batt[b]
        )
    model.soc_dynamics = pyo.Constraint(model.B, model.T, rule=soc_dynamics_rule)
    
    # Availability constraint
    def availability_rule(m, b, t):
        if not state.vehicle_availability[b][t]:
            return m.P_charge[b, t] == 0
        return pyo.Constraint.Skip
    model.availability_con = pyo.Constraint(model.B, model.T, rule=availability_rule)
    
    # Departure SoC requirement (HARD)
    def departure_soc_rule(m, b):
        t_depart = state.departure_times.get(b)
        if t_depart is not None and t_depart < config.n_timesteps:
            return m.SoC[b, t_depart] >= 0.99  # 99% at departure
        return pyo.Constraint.Skip
    model.departure_soc = pyo.Constraint(model.B, rule=departure_soc_rule)
    
    # Charger linking
    def charger_link_rule(m, b, t):
        return m.P_charge[b, t] <= config.charger_power * m.y_charge[b, t]
    model.charger_link = pyo.Constraint(model.B, model.T, rule=charger_link_rule)
    
    # Charger capacity
    def charger_capacity_rule(m, t):
        return sum(m.y_charge[b, t] for b in m.B) <= config.n_chargers
    model.charger_capacity = pyo.Constraint(model.T, rule=charger_capacity_rule)
    
    # Grid power balance
    def grid_balance_rule(m, t):
        return m.P_grid[t] == sum(m.P_charge[b, t] for b in m.B) - m.P_batt[t]
    model.grid_balance = pyo.Constraint(model.T, rule=grid_balance_rule)
    
    # Peak demand tracking
    def peak_tracking_rule(m, t):
        return m.P_peak >= m.P_grid[t]
    model.peak_tracking = pyo.Constraint(model.T, rule=peak_tracking_rule)
    
    # Moving peak limit (from current month)
    def moving_peak_rule(m):
        return m.P_peak >= state.current_month_peak
    model.moving_peak = pyo.Constraint(rule=moving_peak_rule)
    
    # Battery storage dynamics
    def batt_init_rule(m):
        return m.SoC_batt[0] == state.battery_soc
    model.batt_init = pyo.Constraint(rule=batt_init_rule)
    
    def batt_dynamics_rule(m, t):
        if t == 0:
            return pyo.Constraint.Skip
        return m.SoC_batt[t] == m.SoC_batt[t-1] + (
            m.P_batt[t-1] * config.delta_t / config.battery_capacity
        )
    model.batt_dynamics = pyo.Constraint(model.T, rule=batt_dynamics_rule)
    
    # Objective: minimize energy cost + demand charges
    def objective_rule(m):
        energy_cost = sum(
            m.price[t] * m.P_grid[t] * config.delta_t
            for t in m.T
        )
        demand_cost = state.demand_charge_rate * m.P_peak
        return energy_cost + demand_cost
    
    model.objective = pyo.Objective(rule=objective_rule, sense=pyo.minimize)
    
    return model


def solve_model(model: pyo.ConcreteModel, time_limit: float = 30.0) -> dict:
    """Solve the optimization model and return results."""
    import highspy
    
    solver = pyo.SolverFactory('appsi_highs')
    solver.options['time_limit'] = time_limit
    solver.options['mip_rel_gap'] = 0.01  # 1% optimality gap acceptable
    
    result = solver.solve(model, tee=False)
    
    if result.solver.termination_condition != pyo.TerminationCondition.optimal:
        # Accept near-optimal solutions
        if result.solver.termination_condition not in [
            pyo.TerminationCondition.maxTimeLimit,
            pyo.TerminationCondition.feasible
        ]:
            raise RuntimeError(f"Solver failed: {result.solver.termination_condition}")
    
    # Extract solution
    schedule = {}
    for b in model.B:
        schedule[b] = {
            'charging_power': [pyo.value(model.P_charge[b, t]) for t in model.T],
            'soc': [pyo.value(model.SoC[b, t]) for t in model.T],
        }
    
    return {
        'schedule': schedule,
        'battery_dispatch': [pyo.value(model.P_batt[t]) for t in model.T],
        'grid_power': [pyo.value(model.P_grid[t]) for t in model.T],
        'peak_demand': pyo.value(model.P_peak),
        'objective_value': pyo.value(model.objective),
        'solve_time': result.solver.time,
    }
```

**Verification:**
- [ ] Model builds without errors
- [ ] Solver finds feasible solution for test case
- [ ] Solve time < 30 seconds for 20 vehicles, 96 timesteps
- [ ] All departure SoC constraints satisfied

---

### Step 1.2: Solver Performance Optimization

**Objective:** Ensure sub-30-second solve times for production scale.

**Techniques:**
1. **Warm-starting**: Use previous solution as initial guess
2. **Variable fixing**: Fix variables for vehicles currently on route
3. **Symmetry breaking**: Order charger assignments
4. **Tighter bounds**: Update SoC bounds based on current state

**Implementation:**
```python
def warm_start_model(model: pyo.ConcreteModel, previous_solution: dict) -> None:
    """Initialize variables from previous solution."""
    for b in model.B:
        for t in model.T:
            if b in previous_solution['schedule']:
                model.P_charge[b, t].value = previous_solution['schedule'][b]['charging_power'][t]
                model.y_charge[b, t].value = 1 if previous_solution['schedule'][b]['charging_power'][t] > 0.1 else 0
```

**Verification:**
- [ ] Warm-started solve < 10 seconds
- [ ] Solution quality within 2% of cold-start

---

### Step 1.3: Unit Tests for Optimizer

Create `tests/unit/test_optimizer.py`:

```python
"""Unit tests for MILP optimizer."""
import pytest
from src.core.optimizer.milp_model import (
    build_optimization_model, solve_model, DepotState, DepotConfig
)

@pytest.fixture
def simple_depot_config():
    return DepotConfig(
        vehicle_capacities={'bus_1': 324, 'bus_2': 324},
        charger_power=80,
        charger_efficiency=0.95,
        n_chargers=2,
        battery_capacity=500,
        battery_power=100,
        max_site_power=500,
    )

@pytest.fixture
def simple_depot_state(simple_depot_config):
    n_t = simple_depot_config.n_timesteps
    return DepotState(
        vehicle_socs={'bus_1': 0.3, 'bus_2': 0.5},
        battery_soc=0.5,
        prices=[0.10] * n_t,  # flat $0.10/kWh
        demand_charge_rate=15.0,
        current_month_peak=100.0,
        vehicle_availability={
            'bus_1': [True] * n_t,
            'bus_2': [True] * n_t,
        },
        energy_requirements={'bus_1': 200, 'bus_2': 150},
        departure_times={'bus_1': 48, 'bus_2': 60},  # noon and 3pm
    )

def test_model_builds(simple_depot_state, simple_depot_config):
    """Model should build without errors."""
    model = build_optimization_model(simple_depot_state, simple_depot_config)
    assert model is not None

def test_model_solves_feasible(simple_depot_state, simple_depot_config):
    """Model should find feasible solution."""
    model = build_optimization_model(simple_depot_state, simple_depot_config)
    result = solve_model(model, time_limit=60)
    assert result['objective_value'] is not None

def test_departure_soc_satisfied(simple_depot_state, simple_depot_config):
    """All vehicles should be charged by departure."""
    model = build_optimization_model(simple_depot_state, simple_depot_config)
    result = solve_model(model)
    
    for vehicle_id, t_depart in simple_depot_state.departure_times.items():
        soc_at_departure = result['schedule'][vehicle_id]['soc'][t_depart]
        assert soc_at_departure >= 0.98, f"{vehicle_id} not charged: SoC={soc_at_departure}"

def test_solve_time_under_30s(simple_depot_state, simple_depot_config):
    """Solve time should be under 30 seconds."""
    model = build_optimization_model(simple_depot_state, simple_depot_config)
    result = solve_model(model, time_limit=30)
    assert result['solve_time'] < 30
```

**Verification:**
- [ ] All tests pass
- [ ] Test coverage > 80% for optimizer module

---

## PHASE 2: ENERGY CONSUMPTION SURROGATE MODEL

### Step 2.1: Feature Engineering

**Objective:** Implement Gaussian Process surrogate model following Stanford approach.

**Features (from Stanford paper):**
| Feature | Source | Type |
|---------|--------|------|
| Bus size | Fleet config | Categorical |
| Route ID | Schedule | Categorical |
| Average temperature | Weather API | Continuous |
| Max temperature | Weather API | Continuous |
| Min temperature | Weather API | Continuous |
| Daily rain | Weather API | Continuous |
| Heating Degree Days | Derived | Continuous |
| Cooling Degree Days | Derived | Continuous |
| Is school day | Calendar | Binary |
| Solar radiation | Weather API | Continuous |

**Implementation:**

Create `src/core/surrogate/energy_model.py`:

```python
"""Gaussian Process surrogate model for energy consumption prediction."""
from __future__ import annotations

import numpy as np
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from dataclasses import dataclass
import joblib
from pathlib import Path


@dataclass
class PredictionInput:
    """Input features for energy consumption prediction."""
    bus_size: str            # 'large' or 'small'
    route_id: str
    temp_avg_f: float
    temp_max_f: float
    temp_min_f: float
    rain_inches: float
    solar_radiation: float   # cal/cm²
    is_school_day: bool


def compute_degree_days(temp_avg_f: float, base_temp: float = 65.0) -> tuple[float, float]:
    """Compute Heating and Cooling Degree Days."""
    hdd = max(0, base_temp - temp_avg_f)
    cdd = max(0, temp_avg_f - base_temp)
    return hdd, cdd


class EnergySurrogateModel:
    """Gaussian Process model for predicting EV energy consumption."""
    
    def __init__(self, known_routes: list[str]):
        """Initialize model with known route IDs."""
        self.known_routes = known_routes
        
        # Preprocessing pipeline
        categorical_features = ['bus_size', 'route_id']
        numerical_features = [
            'temp_avg_f', 'temp_max_f', 'temp_min_f', 
            'rain_inches', 'solar_radiation', 'hdd', 'cdd', 'is_school_day'
        ]
        
        self.preprocessor = ColumnTransformer(
            transformers=[
                ('cat', OneHotEncoder(handle_unknown='ignore'), categorical_features),
                ('num', StandardScaler(), numerical_features),
            ]
        )
        
        # GP kernel (following Stanford approach)
        kernel = (
            ConstantKernel(1.0, (1e-3, 1e3)) * 
            RBF(length_scale=1.0, length_scale_bounds=(1e-2, 1e2)) +
            WhiteKernel(noise_level=0.1, noise_level_bounds=(1e-5, 1e1))
        )
        
        self.gp = GaussianProcessRegressor(
            kernel=kernel,
            n_restarts_optimizer=5,
            normalize_y=True,
            random_state=42,
        )
        
        self.pipeline = Pipeline([
            ('preprocess', self.preprocessor),
            ('gp', self.gp),
        ])
        
        self._is_fitted = False
    
    def _prepare_features(self, inputs: list[PredictionInput]) -> np.ndarray:
        """Convert PredictionInput list to feature matrix."""
        records = []
        for inp in inputs:
            hdd, cdd = compute_degree_days(inp.temp_avg_f)
            records.append({
                'bus_size': inp.bus_size,
                'route_id': inp.route_id,
                'temp_avg_f': inp.temp_avg_f,
                'temp_max_f': inp.temp_max_f,
                'temp_min_f': inp.temp_min_f,
                'rain_inches': inp.rain_inches,
                'solar_radiation': inp.solar_radiation,
                'hdd': hdd,
                'cdd': cdd,
                'is_school_day': int(inp.is_school_day),
            })
        
        import pandas as pd
        return pd.DataFrame(records)
    
    def fit(self, inputs: list[PredictionInput], energy_kwh: list[float]) -> None:
        """Train the surrogate model on historical data."""
        X = self._prepare_features(inputs)
        y = np.array(energy_kwh)
        
        self.pipeline.fit(X, y)
        self._is_fitted = True
    
    def predict(self, inputs: list[PredictionInput]) -> tuple[np.ndarray, np.ndarray]:
        """Predict energy consumption with uncertainty.
        
        Returns:
            mean: Predicted energy consumption (kWh)
            std: Standard deviation (uncertainty)
        """
        if not self._is_fitted:
            raise RuntimeError("Model not fitted. Call fit() first.")
        
        X = self._prepare_features(inputs)
        X_transformed = self.preprocessor.transform(X)
        
        mean, std = self.gp.predict(X_transformed, return_std=True)
        return mean, std
    
    def get_r2_score(self, inputs: list[PredictionInput], y_true: list[float]) -> float:
        """Compute R² score on validation data."""
        from sklearn.metrics import r2_score
        y_pred, _ = self.predict(inputs)
        return r2_score(y_true, y_pred)
    
    def save(self, path: Path) -> None:
        """Save model to disk."""
        joblib.dump({'pipeline': self.pipeline, 'routes': self.known_routes}, path)
    
    @classmethod
    def load(cls, path: Path) -> 'EnergySurrogateModel':
        """Load model from disk."""
        data = joblib.load(path)
        model = cls(data['routes'])
        model.pipeline = data['pipeline']
        model._is_fitted = True
        return model
```

**Verification:**
- [ ] Model fits on synthetic data
- [ ] R² ≥ 0.85 on held-out test set
- [ ] Predictions return reasonable uncertainty estimates
- [ ] Model serializes/deserializes correctly

---

### Step 2.2: Training Pipeline

Create `src/core/surrogate/training.py`:

```python
"""Training pipeline for energy surrogate model."""
from datetime import datetime, timedelta
from typing import Optional
import asyncpg

from .energy_model import EnergySurrogateModel, PredictionInput


async def fetch_training_data(
    pool: asyncpg.Pool,
    depot_id: str,
    lookback_days: int = 30,
) -> tuple[list[PredictionInput], list[float]]:
    """Fetch training data from database."""
    
    query = """
    SELECT 
        v.vehicle_type as bus_size,
        s.route_id,
        -- Weather features would come from weather table
        -- For MVP, we'll use placeholder
        25.0 as temp_avg_f,
        30.0 as temp_max_f,
        20.0 as temp_min_f,
        0.0 as rain_inches,
        500.0 as solar_radiation,
        true as is_school_day,
        s.estimated_energy_kwh as actual_energy
    FROM schedules s
    JOIN vehicles v ON s.vehicle_id = v.vehicle_id
    WHERE v.depot_id = $1
      AND s.departure_time > NOW() - INTERVAL '%s days'
      AND s.estimated_energy_kwh IS NOT NULL
    """ % lookback_days
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, depot_id)
    
    inputs = []
    energies = []
    for row in rows:
        inputs.append(PredictionInput(
            bus_size=row['bus_size'],
            route_id=row['route_id'],
            temp_avg_f=row['temp_avg_f'],
            temp_max_f=row['temp_max_f'],
            temp_min_f=row['temp_min_f'],
            rain_inches=row['rain_inches'],
            solar_radiation=row['solar_radiation'],
            is_school_day=row['is_school_day'],
        ))
        energies.append(row['actual_energy'])
    
    return inputs, energies


async def train_and_validate(
    pool: asyncpg.Pool,
    depot_id: str,
    training_days: int = 30,
    validation_days: int = 7,
) -> tuple[EnergySurrogateModel, float]:
    """Train model and return R² score on validation set."""
    
    # Fetch training data (last 30 days excluding last 7)
    train_inputs, train_energies = await fetch_training_data(
        pool, depot_id, lookback_days=training_days + validation_days
    )
    
    # Split: first N-7 days for training, last 7 for validation
    split_idx = len(train_inputs) - validation_days * 10  # rough estimate
    
    train_X = train_inputs[:split_idx]
    train_y = train_energies[:split_idx]
    val_X = train_inputs[split_idx:]
    val_y = train_energies[split_idx:]
    
    # Get unique routes
    routes = list(set(inp.route_id for inp in train_X))
    
    # Train model
    model = EnergySurrogateModel(routes)
    model.fit(train_X, train_y)
    
    # Validate
    r2 = model.get_r2_score(val_X, val_y)
    
    return model, r2
```

**Verification:**
- [ ] Training pipeline runs end-to-end
- [ ] R² score logged and tracked
- [ ] Model automatically retrained weekly

---

## PHASE 3: DATA INGESTION & ADAPTERS

### Step 3.1: OCPP Client/Server

**Objective:** Implement OCPP 1.6/2.0.1 integration for charger communication.

Create `src/adapters/ocpp/charge_point.py`:

```python
"""OCPP Charge Point management."""
from datetime import datetime
from typing import Optional
import asyncio
from ocpp.v16 import ChargePoint as CP16
from ocpp.v16 import call, call_result
from ocpp.routing import on
import websockets


class FleetChargePoint(CP16):
    """Custom ChargePoint handler for fleet optimization."""
    
    def __init__(self, id: str, connection, on_status_change=None):
        super().__init__(id, connection)
        self.on_status_change = on_status_change
        self.current_transaction_id: Optional[int] = None
    
    @on('BootNotification')
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
        return call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat(),
            interval=300,  # heartbeat every 5 minutes
            status='Accepted'
        )
    
    @on('StatusNotification')
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        if self.on_status_change:
            await self.on_status_change(self.id, connector_id, status)
        return call_result.StatusNotificationPayload()
    
    @on('MeterValues')
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        # Extract SoC and power from meter values
        for mv in meter_value:
            for sv in mv.get('sampled_value', []):
                if sv.get('measurand') == 'SoC':
                    soc = float(sv['value']) / 100.0
                    # Store SoC update
                elif sv.get('measurand') == 'Power.Active.Import':
                    power_kw = float(sv['value']) / 1000.0
                    # Store power update
        return call_result.MeterValuesPayload()
    
    async def set_charging_profile(
        self,
        connector_id: int,
        charging_schedule: list[dict],  # [{start_period, limit, number_phases}]
    ) -> bool:
        """Send SetChargingProfile to charger."""
        payload = call.SetChargingProfilePayload(
            connector_id=connector_id,
            cs_charging_profiles={
                'charging_profile_id': 1,
                'stack_level': 0,
                'charging_profile_purpose': 'TxProfile',
                'charging_profile_kind': 'Absolute',
                'charging_schedule': {
                    'charging_rate_unit': 'W',
                    'charging_schedule_period': charging_schedule,
                }
            }
        )
        
        response = await self.call(payload)
        return response.status == 'Accepted'
    
    async def remote_start_transaction(self, connector_id: int, id_tag: str) -> bool:
        """Start charging session."""
        payload = call.RemoteStartTransactionPayload(
            connector_id=connector_id,
            id_tag=id_tag,
        )
        response = await self.call(payload)
        return response.status == 'Accepted'
    
    async def remote_stop_transaction(self, transaction_id: int) -> bool:
        """Stop charging session."""
        payload = call.RemoteStopTransactionPayload(transaction_id=transaction_id)
        response = await self.call(payload)
        return response.status == 'Accepted'


class OCPPServer:
    """WebSocket server for OCPP connections."""
    
    def __init__(self, host: str = '0.0.0.0', port: int = 9000):
        self.host = host
        self.port = port
        self.charge_points: dict[str, FleetChargePoint] = {}
    
    async def on_connect(self, websocket, path):
        """Handle new charge point connection."""
        charge_point_id = path.strip('/')
        cp = FleetChargePoint(charge_point_id, websocket)
        self.charge_points[charge_point_id] = cp
        
        try:
            await cp.start()
        finally:
            del self.charge_points[charge_point_id]
    
    async def start(self):
        """Start OCPP WebSocket server."""
        server = await websockets.serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=['ocpp1.6']
        )
        await server.wait_closed()
```

**Verification:**
- [ ] Server accepts OCPP 1.6 connections
- [ ] BootNotification handled correctly
- [ ] SetChargingProfile sends valid message
- [ ] MeterValues parsed and stored

---

### Step 3.2: CAISO Price Feed Adapter

Create `src/adapters/caiso/prices.py`:

```python
"""CAISO price feed adapter."""
from datetime import datetime, timedelta
from typing import Optional
import httpx
from dataclasses import dataclass


@dataclass
class CAISOPrice:
    """CAISO electricity price data."""
    timestamp: datetime
    lmp: float              # Locational Marginal Price ($/MWh)
    energy: float           # Energy component
    congestion: float       # Congestion component  
    loss: float             # Loss component
    node: str               # Pricing node


class CAISOAdapter:
    """Adapter for CAISO OASIS price data."""
    
    BASE_URL = "http://oasis.caiso.com/oasisapi/SingleZip"
    
    def __init__(self, default_node: str = "SLAP_PGAE-APND"):
        self.default_node = default_node
        self.client = httpx.AsyncClient(timeout=30.0)
    
    async def get_day_ahead_prices(
        self,
        start_date: datetime,
        end_date: datetime,
        node: Optional[str] = None,
    ) -> list[CAISOPrice]:
        """Fetch Day-Ahead LMP prices from CAISO OASIS."""
        
        node = node or self.default_node
        
        params = {
            'queryname': 'PRC_LMP',
            'startdatetime': start_date.strftime('%Y%m%dT07:00-0000'),
            'enddatetime': end_date.strftime('%Y%m%dT07:00-0000'),
            'market_run_id': 'DAM',
            'node': node,
            'resultformat': '6',  # CSV format
        }
        
        # Note: CAISO OASIS returns a ZIP file with CSV
        # For MVP, we might use a proxy service or cached data
        
        # Placeholder: return mock TOU prices
        prices = []
        current = start_date
        while current < end_date:
            hour = current.hour
            
            # Mock TOU structure (PG&E E-19)
            if hour in range(16, 21):  # Peak: 4pm-9pm
                lmp = 250.0
            elif hour in range(9, 16) or hour in range(21, 24):  # Partial-peak
                lmp = 150.0
            else:  # Off-peak
                lmp = 80.0
            
            prices.append(CAISOPrice(
                timestamp=current,
                lmp=lmp,
                energy=lmp * 0.8,
                congestion=lmp * 0.15,
                loss=lmp * 0.05,
                node=node,
            ))
            current += timedelta(hours=1)
        
        return prices
    
    async def get_current_price(self, node: Optional[str] = None) -> CAISOPrice:
        """Get current real-time price."""
        now = datetime.utcnow()
        prices = await self.get_day_ahead_prices(now, now + timedelta(hours=1), node)
        return prices[0] if prices else None
```

**Verification:**
- [ ] Adapter returns valid price data
- [ ] TOU periods correctly identified
- [ ] Error handling for API failures

---

### Step 3.3: Weather Adapter (Open-Meteo)

Create `src/adapters/weather/openmeteo.py`:

```python
"""Weather data adapter using Open-Meteo API."""
from datetime import datetime
from dataclasses import dataclass
import httpx


@dataclass
class WeatherData:
    """Weather observation/forecast data."""
    timestamp: datetime
    temperature_f: float
    temperature_max_f: float
    temperature_min_f: float
    precipitation_inches: float
    solar_radiation: float  # W/m² average


class OpenMeteoAdapter:
    """Adapter for Open-Meteo weather API."""
    
    BASE_URL = "https://api.open-meteo.com/v1/forecast"
    
    def __init__(self, latitude: float, longitude: float):
        self.latitude = latitude
        self.longitude = longitude
        self.client = httpx.AsyncClient(timeout=10.0)
    
    async def get_forecast(self, days: int = 7) -> list[WeatherData]:
        """Fetch weather forecast."""
        
        params = {
            'latitude': self.latitude,
            'longitude': self.longitude,
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,shortwave_radiation_sum',
            'hourly': 'temperature_2m',
            'temperature_unit': 'fahrenheit',
            'precipitation_unit': 'inch',
            'forecast_days': days,
        }
        
        response = await self.client.get(self.BASE_URL, params=params)
        response.raise_for_status()
        data = response.json()
        
        results = []
        daily = data.get('daily', {})
        for i, date_str in enumerate(daily.get('time', [])):
            dt = datetime.fromisoformat(date_str)
            results.append(WeatherData(
                timestamp=dt,
                temperature_f=(daily['temperature_2m_max'][i] + daily['temperature_2m_min'][i]) / 2,
                temperature_max_f=daily['temperature_2m_max'][i],
                temperature_min_f=daily['temperature_2m_min'][i],
                precipitation_inches=daily['precipitation_sum'][i] or 0.0,
                solar_radiation=daily['shortwave_radiation_sum'][i] or 0.0,
            ))
        
        return results
```

**Verification:**
- [ ] API calls succeed
- [ ] Data parsed correctly
- [ ] Caching implemented for rate limits

---

## PHASE 4: STATE ASSEMBLER & MPC CONTROLLER

### Step 4.1: State Assembler

Create `src/core/state/assembler.py`:

```python
"""State assembler for optimization inputs."""
from datetime import datetime, timedelta
from typing import Optional
import asyncpg

from ..optimizer.milp_model import DepotState, DepotConfig


class StateAssembler:
    """Assembles current depot state for optimization."""
    
    def __init__(self, pool: asyncpg.Pool, depot_id: str, config: DepotConfig):
        self.pool = pool
        self.depot_id = depot_id
        self.config = config
    
    async def get_current_state(
        self,
        horizon_hours: int = 24,
    ) -> DepotState:
        """Assemble current depot state from all sources."""
        
        now = datetime.utcnow()
        horizon_end = now + timedelta(hours=horizon_hours)
        n_steps = int(horizon_hours / self.config.delta_t)
        
        # Fetch vehicle SoCs from latest telemetry
        vehicle_socs = await self._get_vehicle_socs()
        
        # Fetch battery SoC
        battery_soc = await self._get_battery_soc()
        
        # Fetch prices
        prices = await self._get_prices(now, horizon_end, n_steps)
        
        # Fetch schedules and compute availability
        schedules = await self._get_schedules(now, horizon_end)
        availability = self._compute_availability(schedules, now, n_steps)
        departure_times = self._compute_departure_times(schedules, now)
        energy_requirements = self._compute_energy_requirements(schedules)
        
        # Get current month peak
        current_month_peak = await self._get_current_month_peak()
        
        # Get demand charge rate
        demand_charge_rate = await self._get_demand_charge_rate()
        
        return DepotState(
            vehicle_socs=vehicle_socs,
            battery_soc=battery_soc,
            prices=prices,
            demand_charge_rate=demand_charge_rate,
            current_month_peak=current_month_peak,
            vehicle_availability=availability,
            energy_requirements=energy_requirements,
            departure_times=departure_times,
        )
    
    async def _get_vehicle_socs(self) -> dict[str, float]:
        """Get latest SoC for all vehicles."""
        query = """
        SELECT DISTINCT ON (vehicle_id) 
            vehicle_id::text, soc
        FROM telemetry t
        JOIN vehicles v ON t.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1
        ORDER BY vehicle_id, time DESC
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, self.depot_id)
        return {str(row['vehicle_id']): row['soc'] for row in rows}
    
    async def _get_battery_soc(self) -> float:
        """Get stationary battery SoC."""
        # For MVP, return fixed value or from separate table
        return 0.5
    
    async def _get_prices(
        self, start: datetime, end: datetime, n_steps: int
    ) -> list[float]:
        """Get electricity prices for horizon."""
        query = """
        SELECT time, price_per_kwh
        FROM prices
        WHERE depot_id = $1 AND time >= $2 AND time < $3
        ORDER BY time
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, self.depot_id, start, end)
        
        # Interpolate to match optimization timesteps
        if not rows:
            return [0.15] * n_steps  # Default price
        
        # Simplified: use hourly prices, repeat for 15-min steps
        prices = []
        for row in rows:
            prices.extend([row['price_per_kwh']] * 4)  # 4 x 15-min = 1 hour
        
        return prices[:n_steps]
    
    async def _get_schedules(self, start: datetime, end: datetime) -> list[dict]:
        """Get vehicle schedules."""
        query = """
        SELECT vehicle_id::text, departure_time, return_time, 
               estimated_energy_kwh, route_id
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1 
          AND s.departure_time >= $2 
          AND s.departure_time < $3
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, self.depot_id, start, end)
        return [dict(row) for row in rows]
    
    def _compute_availability(
        self, schedules: list[dict], start: datetime, n_steps: int
    ) -> dict[str, list[bool]]:
        """Compute per-vehicle availability for each timestep."""
        delta_t = timedelta(hours=self.config.delta_t)
        
        # Initialize all vehicles as available
        availability = {
            vid: [True] * n_steps 
            for vid in self.config.vehicle_capacities.keys()
        }
        
        for sched in schedules:
            vid = sched['vehicle_id']
            if vid not in availability:
                continue
                
            dep = sched['departure_time']
            ret = sched['return_time']
            
            for t in range(n_steps):
                step_time = start + t * delta_t
                if dep <= step_time < ret:
                    availability[vid][t] = False
        
        return availability
    
    def _compute_departure_times(
        self, schedules: list[dict], start: datetime
    ) -> dict[str, int]:
        """Compute timestep index for each vehicle's next departure."""
        delta_t = timedelta(hours=self.config.delta_t)
        departures = {}
        
        for sched in schedules:
            vid = sched['vehicle_id']
            dep = sched['departure_time']
            t_idx = int((dep - start) / delta_t)
            
            if vid not in departures or t_idx < departures[vid]:
                departures[vid] = t_idx
        
        return departures
    
    def _compute_energy_requirements(self, schedules: list[dict]) -> dict[str, float]:
        """Compute energy needed for each vehicle's next trip."""
        requirements = {}
        for sched in schedules:
            vid = sched['vehicle_id']
            energy = sched['estimated_energy_kwh'] or 100.0  # default
            if vid not in requirements:
                requirements[vid] = energy
        return requirements
    
    async def _get_current_month_peak(self) -> float:
        """Get maximum grid power this billing month."""
        query = """
        SELECT MAX(objective_value) as peak
        FROM optimization_runs
        WHERE depot_id = $1
          AND date_trunc('month', run_time) = date_trunc('month', NOW())
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, self.depot_id)
        return row['peak'] if row and row['peak'] else 0.0
    
    async def _get_demand_charge_rate(self) -> float:
        """Get demand charge rate ($/kW)."""
        # For MVP, hardcoded PG&E E-19 rate
        return 20.0  # $/kW
```

**Verification:**
- [ ] State assembler returns valid DepotState
- [ ] All data sources integrated
- [ ] Availability correctly computed from schedules

---

### Step 4.2: Re-optimization Trigger Monitor

Create `src/core/state/triggers.py`:

```python
"""Re-optimization trigger monitoring."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
import asyncio


@dataclass
class TriggerConfig:
    """Configuration for re-optimization triggers."""
    soc_deviation_threshold: float = 0.05      # 5%
    price_change_percent: float = 0.25         # 25%
    price_change_absolute: float = 25.0        # $25/MWh
    return_time_deviation_min: float = 15.0    # 15 minutes
    check_interval_sec: float = 60.0           # 1 minute


class TriggerMonitor:
    """Monitors conditions that trigger re-optimization."""
    
    def __init__(
        self,
        config: TriggerConfig,
        on_trigger: callable,  # async callback
    ):
        self.config = config
        self.on_trigger = on_trigger
        self.last_prices: dict[datetime, float] = {}
        self.expected_socs: dict[str, float] = {}
        self.expected_return_times: dict[str, datetime] = {}
        self._running = False
    
    def update_expected_state(
        self,
        expected_socs: dict[str, float],
        expected_return_times: dict[str, datetime],
    ):
        """Update expected state from optimization results."""
        self.expected_socs = expected_socs
        self.expected_return_times = expected_return_times
    
    def update_prices(self, prices: dict[datetime, float]):
        """Update price baseline."""
        self.last_prices = prices
    
    async def check_soc_deviation(
        self, current_socs: dict[str, float]
    ) -> Optional[str]:
        """Check if any vehicle SoC deviates from expected."""
        for vid, current in current_socs.items():
            expected = self.expected_socs.get(vid)
            if expected is not None:
                deviation = abs(current - expected)
                if deviation > self.config.soc_deviation_threshold:
                    return f"SoC deviation: {vid} expected {expected:.2f}, got {current:.2f}"
        return None
    
    async def check_price_change(
        self, current_prices: dict[datetime, float]
    ) -> Optional[str]:
        """Check if prices changed significantly."""
        for ts, current in current_prices.items():
            last = self.last_prices.get(ts)
            if last is not None and last > 0:
                pct_change = abs(current - last) / last
                abs_change = abs(current - last)
                
                # Combined threshold (both must be met)
                if (pct_change > self.config.price_change_percent and
                    abs_change > self.config.price_change_absolute):
                    return f"Price change: {ts} was ${last:.2f}, now ${current:.2f}"
        return None
    
    async def check_return_time_deviation(
        self, actual_return_times: dict[str, datetime]
    ) -> Optional[str]:
        """Check if any vehicle returned significantly late."""
        threshold = timedelta(minutes=self.config.return_time_deviation_min)
        
        for vid, actual in actual_return_times.items():
            expected = self.expected_return_times.get(vid)
            if expected is not None:
                deviation = actual - expected
                if deviation > threshold:
                    return f"Return delay: {vid} expected {expected}, returned {actual}"
        return None
    
    async def run(self):
        """Main monitoring loop."""
        self._running = True
        
        while self._running:
            try:
                # Get current state (would be injected in production)
                # current_socs = await self.get_current_socs()
                # current_prices = await self.get_current_prices()
                # actual_returns = await self.get_actual_returns()
                
                # Check all triggers
                # soc_trigger = await self.check_soc_deviation(current_socs)
                # price_trigger = await self.check_price_change(current_prices)
                # return_trigger = await self.check_return_time_deviation(actual_returns)
                
                # if any([soc_trigger, price_trigger, return_trigger]):
                #     reason = soc_trigger or price_trigger or return_trigger
                #     await self.on_trigger(reason)
                
                pass  # Placeholder for actual implementation
                
            except Exception as e:
                print(f"Trigger monitor error: {e}")
            
            await asyncio.sleep(self.config.check_interval_sec)
    
    def stop(self):
        """Stop monitoring."""
        self._running = False
```

**Verification:**
- [ ] Triggers fire correctly when thresholds exceeded
- [ ] No false positives during normal operation
- [ ] Callbacks invoke re-optimization

---

## PHASE 5: API & INTEGRATION

### Step 5.1: FastAPI Application

Create `src/api/main.py`:

```python
"""FastAPI application for Favonius platform."""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
import asyncpg

from ..core.optimizer.milp_model import build_optimization_model, solve_model
from ..core.state.assembler import StateAssembler
from ..core.optimizer.milp_model import DepotConfig


# Database connection pool
db_pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        dsn="postgresql://user:pass@localhost:5432/favonius"
    )
    yield
    await db_pool.close()


app = FastAPI(
    title="Favonius Energy API",
    version="0.1.0",
    lifespan=lifespan,
)


class OptimizationRequest(BaseModel):
    """Request to run optimization."""
    depot_id: str
    horizon_hours: int = 24
    force: bool = False  # Force re-optimization


class OptimizationResult(BaseModel):
    """Optimization result response."""
    run_id: str
    depot_id: str
    objective_value: float
    solve_time_seconds: float
    peak_demand_kw: float
    schedule: dict


@app.post("/optimize", response_model=OptimizationResult)
async def run_optimization(request: OptimizationRequest):
    """Trigger depot charging optimization."""
    
    # Get depot config (would be from database in production)
    config = DepotConfig(
        vehicle_capacities={'bus_1': 324, 'bus_2': 324},
        charger_power=80,
        charger_efficiency=0.95,
        n_chargers=10,
        battery_capacity=500,
        battery_power=100,
        max_site_power=800,
    )
    
    # Assemble state
    assembler = StateAssembler(db_pool, request.depot_id, config)
    state = await assembler.get_current_state(request.horizon_hours)
    
    # Build and solve model
    model = build_optimization_model(state, config)
    result = solve_model(model, time_limit=30.0)
    
    # Store result
    run_id = await _store_result(request.depot_id, result)
    
    return OptimizationResult(
        run_id=run_id,
        depot_id=request.depot_id,
        objective_value=result['objective_value'],
        solve_time_seconds=result['solve_time'],
        peak_demand_kw=result['peak_demand'],
        schedule=result['schedule'],
    )


async def _store_result(depot_id: str, result: dict) -> str:
    """Store optimization result in database."""
    import uuid
    run_id = str(uuid.uuid4())
    
    query = """
    INSERT INTO optimization_runs 
        (run_id, depot_id, objective_value, solve_time_seconds, schedule_json)
    VALUES ($1, $2, $3, $4, $5)
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            query,
            run_id,
            depot_id,
            result['objective_value'],
            result['solve_time'],
            result,
        )
    
    return run_id


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/depots/{depot_id}/state")
async def get_depot_state(depot_id: str):
    """Get current depot state."""
    config = DepotConfig(
        vehicle_capacities={},
        charger_power=80,
        charger_efficiency=0.95,
        n_chargers=10,
        battery_capacity=500,
        battery_power=100,
        max_site_power=800,
    )
    
    assembler = StateAssembler(db_pool, depot_id, config)
    state = await assembler.get_current_state(24)
    
    return {
        "depot_id": depot_id,
        "vehicle_socs": state.vehicle_socs,
        "battery_soc": state.battery_soc,
        "current_month_peak": state.current_month_peak,
    }
```

**Verification:**
- [ ] API starts without errors
- [ ] `/optimize` endpoint returns valid schedule
- [ ] `/health` returns 200

---

### Step 5.2: Control Loop Implementation

Create `src/core/controller.py`:

```python
"""Main control loop for depot optimization."""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from .optimizer.milp_model import build_optimization_model, solve_model
from .state.assembler import StateAssembler
from .state.triggers import TriggerMonitor, TriggerConfig


class DepotController:
    """Main controller for depot charging optimization."""
    
    def __init__(
        self,
        pool,  # asyncpg.Pool
        depot_id: str,
        config,  # DepotConfig
        ocpp_server,  # OCPPServer
    ):
        self.pool = pool
        self.depot_id = depot_id
        self.config = config
        self.ocpp_server = ocpp_server
        self.assembler = StateAssembler(pool, depot_id, config)
        
        self.trigger_monitor = TriggerMonitor(
            TriggerConfig(),
            on_trigger=self._handle_trigger,
        )
        
        self.last_schedule: Optional[dict] = None
        self.last_run_time: Optional[datetime] = None
        self._running = False
    
    async def _handle_trigger(self, reason: str):
        """Handle re-optimization trigger."""
        print(f"Trigger fired: {reason}")
        await self.run_optimization(reason)
    
    async def run_optimization(self, trigger_reason: str = "scheduled"):
        """Run optimization and dispatch commands."""
        
        # Assemble state
        state = await self.assembler.get_current_state(24)
        
        # Build and solve
        model = build_optimization_model(state, self.config)
        result = solve_model(model, time_limit=30.0)
        
        # Store result
        self.last_schedule = result['schedule']
        self.last_run_time = datetime.utcnow()
        
        # Dispatch commands to chargers
        await self._dispatch_commands(result)
        
        # Update trigger monitor expected state
        expected_socs = {
            vid: sched['soc'][1]  # next timestep SoC
            for vid, sched in result['schedule'].items()
        }
        self.trigger_monitor.update_expected_state(expected_socs, {})
        
        print(f"Optimization complete. Objective: ${result['objective_value']:.2f}")
    
    async def _dispatch_commands(self, result: dict):
        """Send charging commands via OCPP."""
        for vehicle_id, schedule in result['schedule'].items():
            # Find corresponding charge point
            cp = self.ocpp_server.charge_points.get(vehicle_id)
            if cp is None:
                continue
            
            # Build charging schedule for next hour
            charging_schedule = []
            for t, power in enumerate(schedule['charging_power'][:4]):  # 4 x 15min
                if power > 0:
                    charging_schedule.append({
                        'start_period': t * 900,  # seconds
                        'limit': power * 1000,     # Watts
                        'number_phases': 3,
                    })
            
            if charging_schedule:
                success = await cp.set_charging_profile(1, charging_schedule)
                if not success:
                    print(f"Failed to set profile for {vehicle_id}")
    
    async def run(self):
        """Main control loop."""
        self._running = True
        
        # Start trigger monitor
        asyncio.create_task(self.trigger_monitor.run())
        
        while self._running:
            now = datetime.utcnow()
            
            # Run hourly optimization (7am-11pm)
            if 7 <= now.hour <= 23:
                if (self.last_run_time is None or 
                    now - self.last_run_time > timedelta(hours=1)):
                    await self.run_optimization("hourly")
            
            await asyncio.sleep(60)  # Check every minute
    
    def stop(self):
        """Stop controller."""
        self._running = False
        self.trigger_monitor.stop()
```

**Verification:**
- [ ] Control loop runs without errors
- [ ] Hourly optimization triggers correctly
- [ ] OCPP commands dispatched successfully

---

## PHASE 6: TESTING & SIMULATION

### Step 6.1: Simulation Harness

Create `scripts/simulation/depot_sim.py`:

```python
"""Depot simulation for testing optimization."""
from datetime import datetime, timedelta
import asyncio
import random
from dataclasses import dataclass


@dataclass
class VehicleSim:
    """Simulated vehicle."""
    vehicle_id: str
    battery_capacity_kwh: float
    current_soc: float = 0.8
    is_charging: bool = False
    is_on_route: bool = False


class DepotSimulator:
    """Simulates depot operations for testing."""
    
    def __init__(self, n_vehicles: int = 10, n_chargers: int = 5):
        self.vehicles = [
            VehicleSim(
                vehicle_id=f"bus_{i}",
                battery_capacity_kwh=random.choice([180, 324, 313]),
                current_soc=random.uniform(0.3, 0.9),
            )
            for i in range(n_vehicles)
        ]
        self.n_chargers = n_chargers
        self.time = datetime.utcnow()
    
    def step(self, dt_minutes: float = 15):
        """Advance simulation by dt_minutes."""
        self.time += timedelta(minutes=dt_minutes)
        
        for v in self.vehicles:
            if v.is_charging:
                # Charge at 80kW for 15 min = 20 kWh
                energy_added = 80 * (dt_minutes / 60)
                v.current_soc = min(1.0, v.current_soc + energy_added / v.battery_capacity_kwh)
            
            if v.is_on_route:
                # Consume energy
                energy_used = random.uniform(10, 30)  # kWh per 15 min
                v.current_soc = max(0.1, v.current_soc - energy_used / v.battery_capacity_kwh)
    
    def get_state(self) -> dict:
        """Get current simulation state."""
        return {
            "time": self.time.isoformat(),
            "vehicles": [
                {
                    "id": v.vehicle_id,
                    "soc": v.current_soc,
                    "is_charging": v.is_charging,
                    "is_on_route": v.is_on_route,
                }
                for v in self.vehicles
            ]
        }
    
    def apply_schedule(self, schedule: dict):
        """Apply optimization schedule."""
        for vid, sched in schedule.items():
            v = next((v for v in self.vehicles if v.vehicle_id == vid), None)
            if v and sched['charging_power'][0] > 0:
                v.is_charging = True
            elif v:
                v.is_charging = False


async def run_simulation():
    """Run simulation with optimization."""
    sim = DepotSimulator(n_vehicles=5)
    
    for step in range(96):  # 24 hours
        print(f"Step {step}: {sim.time}")
        
        # Every 4 steps (hourly), run optimization
        if step % 4 == 0:
            # Would call optimizer here
            pass
        
        sim.step()
        state = sim.get_state()
        print(f"  Avg SoC: {sum(v['soc'] for v in state['vehicles']) / len(state['vehicles']):.2f}")


if __name__ == "__main__":
    asyncio.run(run_simulation())
```

**Verification:**
- [ ] Simulation runs 24-hour scenario
- [ ] Optimization integrates with simulation
- [ ] Results match expected behavior

---

### Step 6.2: Integration Tests

Create `tests/integration/test_full_pipeline.py`:

```python
"""Integration tests for full optimization pipeline."""
import pytest
import asyncio
from datetime import datetime

from src.core.optimizer.milp_model import (
    build_optimization_model, solve_model, DepotState, DepotConfig
)
from src.core.surrogate.energy_model import EnergySurrogateModel, PredictionInput


@pytest.fixture
def realistic_depot():
    """Realistic depot configuration."""
    config = DepotConfig(
        vehicle_capacities={f'bus_{i}': 324 for i in range(20)},
        charger_power=80,
        charger_efficiency=0.95,
        n_chargers=10,
        battery_capacity=1000,
        battery_power=200,
        max_site_power=1200,
    )
    
    n_t = config.n_timesteps
    state = DepotState(
        vehicle_socs={f'bus_{i}': 0.4 + i * 0.02 for i in range(20)},
        battery_soc=0.5,
        prices=[0.10 if t < 32 else 0.25 if t < 64 else 0.15 for t in range(n_t)],
        demand_charge_rate=20.0,
        current_month_peak=400.0,
        vehicle_availability={f'bus_{i}': [True] * n_t for i in range(20)},
        energy_requirements={f'bus_{i}': 200 for i in range(20)},
        departure_times={f'bus_{i}': 24 + i % 12 for i in range(20)},
    )
    
    return config, state


def test_20_vehicle_solve_time(realistic_depot):
    """20 vehicles should solve in under 30 seconds."""
    config, state = realistic_depot
    
    model = build_optimization_model(state, config)
    result = solve_model(model, time_limit=30)
    
    assert result['solve_time'] < 30
    assert result['objective_value'] is not None


def test_all_departures_satisfied(realistic_depot):
    """All vehicles must be charged by departure."""
    config, state = realistic_depot
    
    model = build_optimization_model(state, config)
    result = solve_model(model)
    
    for vid, t_dep in state.departure_times.items():
        soc = result['schedule'][vid]['soc'][t_dep]
        assert soc >= 0.95, f"{vid} not charged: {soc}"


def test_peak_demand_tracking(realistic_depot):
    """Peak demand should respect current month peak."""
    config, state = realistic_depot
    
    model = build_optimization_model(state, config)
    result = solve_model(model)
    
    # Peak should be at least current month peak
    assert result['peak_demand'] >= state.current_month_peak


def test_surrogate_model_integration():
    """Surrogate model should integrate with optimizer."""
    # Create synthetic training data
    inputs = [
        PredictionInput('large', 'route_1', 70, 80, 60, 0, 500, True)
        for _ in range(100)
    ]
    energies = [150 + i * 0.5 for i in range(100)]
    
    model = EnergySurrogateModel(['route_1', 'route_2'])
    model.fit(inputs, energies)
    
    # Predict
    test_input = [PredictionInput('large', 'route_1', 75, 85, 65, 0.1, 400, False)]
    mean, std = model.predict(test_input)
    
    assert mean[0] > 100  # Should be reasonable
    assert std[0] > 0     # Should have uncertainty
```

**Verification:**
- [ ] All integration tests pass
- [ ] Performance benchmarks met
- [ ] Edge cases handled

---

## PHASE 7: DEPLOYMENT & MONITORING

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

  api:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      DATABASE_URL: postgresql://favonius:${DB_PASSWORD}@timescaledb:5432/favonius
      OCPP_PORT: 9000
    ports:
      - "8000:8000"
      - "9000:9000"
    depends_on:
      - timescaledb

  ocpp_simulator:
    build:
      context: .
      dockerfile: Dockerfile.simulator
    environment:
      OCPP_SERVER: ws://api:9000
    depends_on:
      - api

volumes:
  timescale_data:
```

Create `Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy project files
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY src/ ./src/
COPY config/ ./config/

CMD ["uv", "run", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Verification:**
- [ ] `docker-compose up` starts all services
- [ ] API accessible at http://localhost:8000
- [ ] OCPP server accepts connections at ws://localhost:9000

---

### Step 7.2: Monitoring Setup

Create `src/monitoring/metrics.py`:

```python
"""Prometheus metrics for monitoring."""
from prometheus_client import Counter, Histogram, Gauge

# Optimization metrics
OPTIMIZATION_RUNS = Counter(
    'favonius_optimization_runs_total',
    'Total number of optimization runs',
    ['depot_id', 'trigger_reason']
)

OPTIMIZATION_DURATION = Histogram(
    'favonius_optimization_duration_seconds',
    'Optimization solve time',
    ['depot_id'],
    buckets=[1, 5, 10, 20, 30, 60]
)

OPTIMIZATION_OBJECTIVE = Gauge(
    'favonius_optimization_objective_value',
    'Last optimization objective value',
    ['depot_id']
)

# Fleet metrics
VEHICLE_SOC = Gauge(
    'favonius_vehicle_soc',
    'Vehicle state of charge',
    ['depot_id', 'vehicle_id']
)

GRID_POWER = Gauge(
    'favonius_grid_power_kw',
    'Current grid power draw',
    ['depot_id']
)

PEAK_DEMAND = Gauge(
    'favonius_peak_demand_kw',
    'Current month peak demand',
    ['depot_id']
)
```

**Verification:**
- [ ] Metrics endpoint exposed at `/metrics`
- [ ] Grafana dashboard created
- [ ] Alerts configured for optimization failures

---

## Development Milestones Checklist

### Milestone 1: Core Optimization Engine ✓
- [ ] MILP model implemented
- [ ] Solver integration complete
- [ ] Unit tests passing
- [ ] Solve time < 30s for 20 vehicles

### Milestone 2: Data Infrastructure ✓
- [ ] TimescaleDB schema deployed
- [ ] OCPP adapter functional
- [ ] Price feed adapter functional
- [ ] Weather adapter functional

### Milestone 3: State Assembler & Triggers ✓
- [ ] State assembler integrates all sources
- [ ] Trigger monitor detects conditions
- [ ] Re-optimization fires correctly

### Milestone 4: Surrogate Model ✓
- [ ] Gaussian Process model trained
- [ ] R² ≥ 0.85 on validation
- [ ] Predictions integrate with optimizer

### Milestone 5: API & Control Loop ✓
- [ ] FastAPI endpoints functional
- [ ] Hourly optimization runs
- [ ] OCPP commands dispatched

### Milestone 6: Integration & Simulation ✓
- [ ] 24-hour simulation passes
- [ ] All integration tests pass
- [ ] Performance benchmarks met

### Milestone 7: Deployment ✓
- [ ] Docker containers built
- [ ] Compose stack running
- [ ] Monitoring operational

---

## Post-MVP Roadmap

1. **V2G Support**: Add discharge capabilities and grid services
2. **Multi-depot Coordination**: Inter-depot messaging and vehicle handoff
3. **Advanced Forecasting**: RL-based predictions, if needed
4. **Solar Integration**: Re-add solar predictor when needed
5. **Customer Dashboard**: Real-time visualization and ROI tracking
