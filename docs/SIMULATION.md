# Simulation Harness Documentation

## Overview

The simulation harness provides a comprehensive testing and validation framework for the Favonius Energy depot optimization platform. It simulates vehicle charging, route operations, price patterns, and battery storage to enable end-to-end testing of the optimization pipeline.

## Architecture

### Components

1. **DepotSimulator**: Core simulation engine
   - Manages vehicle fleet and charging
   - Tracks routes and vehicle availability
   - Simulates price patterns (TOU, CAISO)
   - Manages battery storage dispatch

2. **SimulationOptimizer**: Optimizer integration
   - Converts simulator state to `DepotState`
   - Runs optimization with warm-starting
   - Applies optimization results to simulator

3. **Scenario Generators**: Pre-configured test scenarios
   - Morning rush scenarios
   - Price spike scenarios
   - SoC deviation scenarios
   - Demand charge scenarios
   - Inter-depot handoff scenarios

4. **Metrics Collection**: Performance and cost tracking
   - Energy costs
   - Demand charges
   - Peak demand
   - Solve times
   - Cost savings vs. baseline

## Usage

### Basic Simulation

```python
from scripts.simulation.depot_sim import DepotSimulator, SimulationOptimizer
from scripts.simulation.scenarios import morning_rush_scenario
from src.core.models import DepotConfig

# Create scenario
sim = morning_rush_scenario(n_vehicles=10, n_chargers=5)

# Create depot config
config = DepotConfig(
    vehicle_capacities={v.vehicle_id: v.battery_capacity_kwh for v in sim.vehicles},
    vehicle_max_charge_kw={v.vehicle_id: 150.0 for v in sim.vehicles},  # Max charge rate per vehicle
    charger_groups={80.0: 5},  # 5 chargers at 80kW each
    charger_efficiency=0.95,
    charger_vehicle_access={},  # Empty = all vehicles can access all chargers
    battery_capacity=500.0,
    battery_power=100.0,
    max_site_power=800.0,
)

# Create optimizer
optimizer = SimulationOptimizer(sim, config)

# Run 24-hour simulation
for step in range(96):  # 96 steps = 24 hours
    if step % 4 == 0:  # Hourly optimization
        result = await optimizer.optimize(horizon_hours=24)
        optimizer.apply_result(result)
    sim.step()

# Print summary
sim.print_summary()
```

### Command-Line Interface

#### Run Simulation

```bash
python -m scripts.simulation.cli run \
    --scenario morning_rush \
    --fleet-size 10 \
    --chargers 5 \
    --steps 96 \
    --output metrics.json \
    --verbose
```

#### Run Benchmarks

```bash
python -m scripts.simulation.cli benchmark \
    --fleet-size 20 \
    --runs 5 \
    --output benchmark_results.json
```

#### Validate Acceptance Tests

```bash
python -m scripts.simulation.cli validate \
    --acceptance-test AT-01 \
    --output validation_results.json
```

## Available Scenarios

### Morning Rush Scenario

Multiple vehicles departing early morning (6-8 AM) requiring full charge.

```python
from scripts.simulation.scenarios import morning_rush_scenario

sim = morning_rush_scenario(
    n_vehicles=10,
    n_chargers=5,
    departure_hour=6,
)
```

### Price Spike Scenario

Price spike during simulation to test re-optimization triggers.

```python
from scripts.simulation.scenarios import price_spike_scenario

sim = price_spike_scenario(
    n_vehicles=10,
    n_chargers=5,
    spike_time=datetime(2025, 1, 1, 14, 0),  # 2 PM
    spike_price=0.25,  # $0.25/kWh
)
```

### SoC Deviation Scenario

Vehicle SoC deviates from expected to test trigger handling.

```python
from scripts.simulation.scenarios import soc_deviation_scenario

sim = soc_deviation_scenario(
    n_vehicles=10,
    n_chargers=5,
    deviation_amount=0.08,  # 8% deviation
)
```

### Demand Charge Scenario

High demand charge scenario to test demand reduction optimization.

```python
from scripts.simulation.scenarios import demand_charge_scenario

sim = demand_charge_scenario(
    n_vehicles=15,
    n_chargers=8,
)
```

### Realistic Depot Scenario

Comprehensive scenario with multiple routes, varied SoCs, and realistic price patterns.

```python
from scripts.simulation.scenarios import realistic_depot_scenario

sim = realistic_depot_scenario(
    n_vehicles=20,
    n_chargers=10,
)
```

## Metrics and Reporting

### Metrics Collection

The simulator automatically tracks:

- **Total energy cost**: Sum of energy costs over simulation
- **Total demand cost**: Sum of demand charges
- **Peak demand**: Maximum grid power draw
- **Optimization count**: Number of optimizations run
- **Total solve time**: Cumulative solve time
- **Average solve time**: Average solve time per optimization
- **Vehicles ready at departure**: Count of vehicles meeting SoC requirements
- **Vehicles not ready**: Count of vehicles failing to meet requirements

### Accessing Metrics

```python
# Get metrics
metrics = sim.get_metrics()
print(f"Total cost: ${metrics.total_energy_cost + metrics.total_demand_cost:.2f}")
print(f"Peak demand: {metrics.peak_demand_kw:.2f} kW")
print(f"Average solve time: {metrics.avg_solve_time:.2f}s")
```

### Cost Savings Calculation

```python
# Calculate savings vs. unmanaged baseline
unmanaged_cost = 5000.0  # Estimated unmanaged cost
savings = sim.get_cost_savings(unmanaged_cost)
print(f"Savings: ${savings['savings']:.2f} ({savings['savings_percent']:.1f}%)")
```

### Exporting Metrics

```python
# Export to JSON
sim.export_metrics("metrics.json", format="json")

# Export to CSV
sim.export_metrics("metrics.csv", format="csv")
```

## Integration Tests

### Full Pipeline Tests

Located in `tests/integration/test_full_pipeline.py`:

- `test_full_optimization_cycle`: Complete optimization cycle
- `test_20_vehicle_solve_time`: Performance benchmark
- `test_all_departures_satisfied`: Constraint validation
- `test_peak_demand_tracking`: Demand charge tracking
- `test_grid_power_balance`: Power balance validation
- `test_charger_capacity_constraint`: Charger capacity validation
- `test_battery_soc_bounds`: Battery SoC bounds validation
- `test_warm_start_performance`: Warm-start speedup
- `test_surrogate_model_integration`: Surrogate model integration
- `test_trigger_based_reoptimization`: Trigger-based re-optimization

### Simulation-Based Acceptance Tests

Located in `tests/integration/test_simulation_acceptance.py`:

- `test_at01_end_to_end_simulation`: AT-01 validation
- `test_at02_demand_charge_reduction`: AT-02 validation
- `test_at03_price_spike_reoptimization`: AT-03 validation
- `test_at04_soc_deviation_handling`: AT-04 validation
- `test_at05_return_time_deviation_handling`: AT-05 validation
- `test_at06_inter_depot_handoff`: AT-06 validation
- `test_at07_building_load_integration`: AT-07 validation
- `test_unmanaged_vs_optimized_comparison`: Cost savings validation

### Running Tests

```bash
# Run all integration tests
pytest tests/integration/test_full_pipeline.py -v

# Run acceptance tests
pytest tests/integration/test_simulation_acceptance.py -v -m acceptance

# Run performance benchmarks
pytest tests/integration/test_performance_benchmarks.py -v -m benchmark
```

## Performance Benchmarks

### Benchmark Tests

Located in `tests/integration/test_performance_benchmarks.py`:

- `test_solve_time_10_vehicles`: 10-vehicle benchmark
- `test_solve_time_20_vehicles`: 20-vehicle benchmark (PRD target)
- `test_solve_time_50_vehicles`: 50-vehicle scalability test
- `test_memory_usage_20_vehicles`: Memory usage validation
- `test_warm_start_speedup`: Warm-start performance
- `test_optimization_frequency_impact`: Frequency impact
- `test_solve_time_consistency`: Consistency validation
- `test_peak_demand_optimization`: Peak demand reduction
- `test_objective_value_consistency`: Objective consistency

### PRD Performance Targets

- **Solve time**: < 60 seconds for 20 vehicles (PRD Section 8.3)
- **Memory usage**: < 2 GB for 20 vehicles
- **Warm-start speedup**: > 3x faster than cold-start (PRD Section 8.5)
- **Optimality gap**: < 1% for deployment

## Examples

### Example 1: Morning Rush Validation

```python
import asyncio
from scripts.simulation.scenarios import morning_rush_scenario
from scripts.simulation.depot_sim import SimulationOptimizer
from src.core.models import DepotConfig

async def main():
    # Create scenario
    sim = morning_rush_scenario(n_vehicles=10, n_chargers=5)
    
    # Create config
    config = DepotConfig(
        vehicle_capacities={v.vehicle_id: v.battery_capacity_kwh for v in sim.vehicles},
        vehicle_max_charge_kw={v.vehicle_id: 150.0 for v in sim.vehicles},
        charger_groups={80.0: 5},  # 5 chargers at 80kW each
        charger_efficiency=0.95,
        charger_vehicle_access={},  # Empty = all vehicles can access all chargers
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )
    
    # Run simulation
    optimizer = SimulationOptimizer(sim, config)
    for step in range(96):
        if step % 4 == 0:
            result = await optimizer.optimize(horizon_hours=24)
            optimizer.apply_result(result)
        sim.step()
    
    # Validate
    assert sim.metrics.vehicles_not_ready == 0
    sim.print_summary()

asyncio.run(main())
```

### Example 2: Price Spike Testing

```python
from datetime import datetime, timedelta
from scripts.simulation.scenarios import price_spike_scenario
from scripts.simulation.depot_sim import SimulationOptimizer
from src.core.models import DepotConfig

# Create scenario with price spike at 2 PM
spike_time = datetime.utcnow().replace(hour=14, minute=0, second=0)
sim = price_spike_scenario(
    n_vehicles=10,
    n_chargers=5,
    spike_time=spike_time,
    spike_price=0.25,
)

# Run simulation and observe re-optimization behavior
# ...
```

### Example 3: Cost Savings Analysis

```python
from scripts.simulation.scenarios import demand_charge_scenario
from scripts.simulation.depot_sim import SimulationOptimizer
from src.core.models import DepotConfig

# Create scenario
sim = demand_charge_scenario(n_vehicles=15, n_chargers=8)

# Run optimized simulation
# ...

# Calculate unmanaged baseline
unmanaged_peak = 15 * 80.0  # All vehicles charge simultaneously
unmanaged_cost = unmanaged_peak * 20.0 * 24  # Simplified

# Get savings
savings = sim.get_cost_savings(unmanaged_cost)
print(f"Cost savings: ${savings['savings']:.2f} ({savings['savings_percent']:.1f}%)")
```

## Troubleshooting

### Common Issues

1. **Optimization fails**: Check that vehicle capacities match simulator vehicles
2. **Solve time exceeds limit**: Reduce fleet size or increase time limit
3. **Vehicles not ready**: Check route schedules and departure times
4. **Metrics not updating**: Ensure `update_metrics()` is called after optimization

### Debug Mode

Enable verbose output in CLI:

```bash
python -m scripts.simulation.cli run --scenario morning_rush --verbose
```

Or in code:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## References

- Development Plan Step 6.1: Simulation Harness
- Development Plan Step 6.2: Integration Tests
- PRD Section 11.1: MVP Acceptance Tests — [PRD_v2_7_Building_Integration.md](PRD_v2_7_Building_Integration.md)
- PRD Section 11.3: Integration Test Requirements
- PRD Section 8.5: Performance Targets

