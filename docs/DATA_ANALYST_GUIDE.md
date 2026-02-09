# Data Analyst Guide: Optimization Engine Testing

## Core Optimization Engine Files

### Primary Files
- **`src/core/optimizer/milp_model.py`** - MILP formulation, model building, constraints
- **`src/core/optimizer/solver.py`** - Solver wrapper (Gurobi/HiGHS), solve execution
- **`src/core/optimizer/optimize()`** - High-level optimization function (in `milp_model.py`)
- **`src/core/optimizer/allocator.py`** - Post-optimization charger allocation
- **`src/core/optimizer/warm_start.py`** - Warm-starting from previous solutions
- **`src/core/optimizer/exceptions.py`** - Error types

### Data Models
- **`src/core/models.py`** - `DepotConfig`, `DepotState`, `OptimizationResult` (critical for understanding inputs/outputs)

## Data Flow: State Assembly → Optimization → Dispatch

### State Assembly (Input Preparation)
- **`src/core/state/assembler.py`** - Assembles `DepotState` from database
  - `get_current_state()` - Main entry point
  - Queries: vehicle SoCs, prices, schedules, building load, battery state

### Controller (Orchestration)
- **`src/core/controller.py`** - Main control loop
  - `run_optimization()` - Triggers optimization, handles retries
  - `_dispatch_commands()` - Sends OCPP commands to chargers
  - `_store_result()` - Persists results to database

### API Integration
- **`src/api/main.py`** - REST API endpoints
  - `POST /optimize` - Manual optimization trigger
  - `GET /depots/{id}/state` - Current depot state
  - `GET /depots/{id}/schedule` - Latest optimization result

## Testing Files

### Unit Tests
- **`tests/unit/test_optimizer.py`** - Core optimizer tests (constraints, feasibility, edge cases)
- **`tests/unit/test_optimizer_exceptions.py`** - Error handling tests

### Integration Tests
- **`tests/integration/test_state_to_optimizer.py`** - State assembly → optimization flow
- **`tests/integration/test_optimizer_to_ocpp_flow.py`** - Optimization → OCPP dispatch
- **`tests/integration/test_trigger_to_optimization_flow.py`** - Trigger → optimization flow
- **`tests/integration/test_optimizer_edge_cases.py`** - Edge cases and error scenarios

### Performance Tests
- **`tests/performance/test_optimizer_performance.py`** - Solve time benchmarks
- **`tests/performance/test_optimizer_benchmarks.py`** - Scalability tests

### Acceptance Tests
- **`tests/integration/test_acceptance_at*.py`** - PRD acceptance criteria validation
  - AT-01: End-to-end optimization
  - AT-02: Demand charge reduction
  - AT-03: Price spike re-optimization
  - AT-04: SoC deviation handling
  - AT-05: Return time deviation
  - AT-06: Inter-depot handoff
  - AT-07: Building load integration

## Simulation Tools

### CLI Tool
- **`scripts/simulation/cli.py`** - Command-line interface for testing
  ```bash
  python -m scripts.simulation.cli run --scenario realistic --fleet-size 10
  python -m scripts.simulation.cli benchmark --fleet-size 20 --runs 5
  python -m scripts.simulation.cli validate --acceptance-test AT-01
  ```

### Simulation Components
- **`scripts/simulation/depot_sim.py`** - Depot simulator
- **`scripts/simulation/scenarios.py`** - Pre-built test scenarios

## Key Concepts for Testing

### Optimization Inputs (`DepotState`)
- `vehicle_socs` - Current SoC per vehicle (0-1)
- `prices` - Electricity prices per timestep ($/kWh)
- `vehicle_availability` - Boolean matrix (vehicle × timestep)
- `departure_times` - Timestep index when vehicle departs
- `building_power` - Building load per timestep (kW) - **REQUIRED**
- `battery_soc` - Stationary battery SoC (0-1)
- `demand_charge_rate` - Demand charge rate ($/kW)
- `current_month_peak` - Current month peak demand (kW)

### Optimization Outputs (`OptimizationResult`)
- `schedule` - Charging schedule per vehicle: `{vehicle_id: {charging_power: [], soc: []}}`
- `battery_dispatch` - Battery power per timestep (+discharge, -charge)
- `grid_power` - Grid power per timestep (kW)
- `peak_demand_kw` - Peak demand (kW)
- `objective_value` - Total cost ($)
- `solve_time_s` - Solve time (seconds)
- `solver_used` - 'gurobi' or 'highs'

### Configuration (`DepotConfig`)
- `vehicle_capacities` - Battery capacity per vehicle (kWh)
- `vehicle_max_charge_kw` - Max charge rate per vehicle (kW)
- `charger_groups` - `{rated_kw: count}` - Charger aggregation
- `battery_capacity` - Stationary battery capacity (kWh)
- `battery_power` - Stationary battery power (kW)
- `max_site_power` - Site power limit (kW)

## Running Tests

### Quick Test Commands
```bash
# Unit tests
pytest tests/unit/test_optimizer.py -v

# Integration tests
pytest tests/integration/test_state_to_optimizer.py -v

# Performance benchmarks
pytest tests/performance/test_optimizer_performance.py -v

# Acceptance tests
pytest tests/integration/test_acceptance_at01.py -v
```

### Test Fixtures
- **`tests/fixtures/realistic_depot.py`** - Reusable test fixtures
- **`tests/conftest.py`** - Pytest configuration and shared fixtures

## Important Constraints

### Hard Constraints (Never Relaxed)
- **Departure SoC ≥ 99%** - Vehicles must be fully charged at departure
- **Solve time < 60 seconds** - Required for production
- **Building load REQUIRED** - Must be included in grid power calculation

### Solver Configuration
- Primary: Gurobi (with license check)
- Fallback: HiGHS (automatic on Gurobi failure)
- Time limit: 60 seconds (configurable)
- MIP gap: 1% (0.01)

## Debugging Tips

1. **Check solver used**: `result.solver_used` - 'gurobi' or 'highs'
2. **Validate constraints**: Check `departure_times` and SoC at departure
3. **State validation**: Ensure `building_power` is non-empty
4. **Model infeasibility**: Check vehicle availability vs. departure requirements
5. **Performance**: Monitor `solve_time_s` - should be < 60s for 20 vehicles

## Database Tables (for State Assembly)

- `telemetry` - Vehicle SoC measurements
- `prices` - Electricity prices
- `schedules` - Vehicle departure/return times
- `building_load` - Building power consumption
- `optimization_runs` - Optimization results storage
- `interdepot_messages` - Inter-depot handoff messages
  - WebSocket Handler writes telemetry into this unified `telemetry` table for trial deployments.

## Next Steps

1. Start with `tests/unit/test_optimizer.py` to understand basic functionality
2. Review `tests/integration/test_state_to_optimizer.py` for data flow
3. Run simulation CLI: `python -m scripts.simulation.cli run --scenario realistic`
4. Examine `OptimizationResult` structure in test outputs
5. Check `src/core/optimizer/milp_model.py` for constraint details
