"""Performance benchmarking suite for optimizer.

Reference: PRD Section 8.3, Development plan Step 1.2
"""

import pytest
import time
from uuid import uuid4

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize


@pytest.fixture
def benchmark_depot_config():
    """20-vehicle depot configuration for benchmarking."""
    vehicle_ids = [f'bus_{i}' for i in range(20)]
    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 10},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=1000.0,
        battery_power=200.0,
        max_site_power=1200.0,
    )


@pytest.fixture
def benchmark_depot_state(benchmark_depot_config):
    """20-vehicle depot state with TOU prices."""
    n_t = benchmark_depot_config.n_timesteps
    # TOU prices: off-peak $0.10, partial-peak $0.15, peak $0.25
    prices = []
    for t in range(n_t):
        hour = (t * 0.25) % 24
        if 16 <= hour < 21:  # Peak: 4pm-9pm
            prices.append(0.25)
        elif 9 <= hour < 16 or 21 <= hour < 24:  # Partial-peak
            prices.append(0.15)
        else:  # Off-peak
            prices.append(0.10)

    return DepotState(
        vehicle_socs={f'bus_{i}': 0.4 + i * 0.02 for i in range(20)},
        battery_soc=0.5,
        prices=prices,
        demand_charge_rate=20.0,
        current_month_peak=400.0,
        vehicle_availability={
            f'bus_{i}': [True] * n_t for i in range(20)
        },
        energy_requirements={f'bus_{i}': 200.0 for i in range(20)},
        departure_times={f'bus_{i}': 24 + i % 12 for i in range(20)},
        building_power=[100.0] * n_t,
    )


@pytest.mark.slow
@pytest.mark.performance
def test_cold_start_benchmark_20_vehicles(
    benchmark_depot_state, benchmark_depot_config
):
    """Benchmark cold-start solve time for 20 vehicles."""
    start_time = time.time()
    result = optimize(benchmark_depot_state, benchmark_depot_config, time_limit=60.0)
    elapsed = time.time() - start_time

    assert result.status == 'completed'
    assert result.solve_time < 30.0, f"Solve time: {result.solve_time:.2f}s"
    assert elapsed < 35.0, f"Total time: {elapsed:.2f}s"

    print(f"\nCold-start benchmark (20 vehicles):")
    print(f"  Solve time: {result.solve_time:.2f}s")
    print(f"  Total time: {elapsed:.2f}s")
    print(f"  Objective: ${result.objective_value:.2f}")


@pytest.mark.slow
@pytest.mark.performance
def test_warm_start_benchmark_20_vehicles(
    benchmark_depot_state, benchmark_depot_config
):
    """Benchmark warm-start solve time for 20 vehicles."""
    # Cold-start first
    cold_result = optimize(
        benchmark_depot_state, benchmark_depot_config, time_limit=60.0
    )

    # Warm-start
    start_time = time.time()
    warm_result = optimize(
        benchmark_depot_state,
        benchmark_depot_config,
        time_limit=60.0,
        previous_result=cold_result,
    )
    elapsed = time.time() - start_time

    assert warm_result.status == 'completed'
    assert warm_result.solve_time < 10.0, f"Warm-start solve time: {warm_result.solve_time:.2f}s"

    # Calculate speedup
    speedup = cold_result.solve_time / warm_result.solve_time
    assert speedup > 3.0, f"Speedup {speedup:.2f}x < 3x"

    print(f"\nWarm-start benchmark (20 vehicles):")
    print(f"  Cold-start time: {cold_result.solve_time:.2f}s")
    print(f"  Warm-start time: {warm_result.solve_time:.2f}s")
    print(f"  Speedup: {speedup:.2f}x")
    print(f"  Total time: {elapsed:.2f}s")
    print(f"  Objective: ${warm_result.objective_value:.2f}")


@pytest.mark.slow
@pytest.mark.performance
def test_optimization_techniques_combined(
    benchmark_depot_state, benchmark_depot_config
):
    """Test that all optimizations together achieve < 30s for 20 vehicles."""
    # This test verifies that variable fixing, symmetry breaking, and tighter bounds
    # are all working together
    result = optimize(benchmark_depot_state, benchmark_depot_config, time_limit=30.0)

    assert result.status == 'completed'
    assert result.solve_time < 30.0, f"Solve time: {result.solve_time:.2f}s"

    # Verify all constraints satisfied
    for vehicle_id, t_dep in benchmark_depot_state.departure_times.items():
        soc = result.schedule[vehicle_id]['soc'][t_dep]
        assert soc >= 0.98, f"{vehicle_id} not charged: {soc:.3f}"

    print(f"\nCombined optimizations benchmark:")
    print(f"  Solve time: {result.solve_time:.2f}s")
    print(f"  Objective: ${result.objective_value:.2f}")
    print(f"  Peak demand: {result.peak_demand:.2f}kW")


@pytest.mark.slow
@pytest.mark.performance
def test_performance_metrics_logging(
    benchmark_depot_state, benchmark_depot_config
):
    """Log performance metrics for monitoring."""
    # Cold-start
    cold_start = time.time()
    cold_result = optimize(
        benchmark_depot_state, benchmark_depot_config, time_limit=60.0
    )
    cold_elapsed = time.time() - cold_start

    # Warm-start
    warm_start = time.time()
    warm_result = optimize(
        benchmark_depot_state,
        benchmark_depot_config,
        time_limit=60.0,
        previous_result=cold_result,
    )
    warm_elapsed = time.time() - warm_start

    # Log metrics
    metrics = {
        'cold_start_solve_time': cold_result.solve_time,
        'cold_start_total_time': cold_elapsed,
        'warm_start_solve_time': warm_result.solve_time,
        'warm_start_total_time': warm_elapsed,
        'speedup': cold_result.solve_time / warm_result.solve_time,
        'objective_cold': cold_result.objective_value,
        'objective_warm': warm_result.objective_value,
        'quality_ratio': warm_result.objective_value / cold_result.objective_value,
    }

    print(f"\nPerformance Metrics:")
    for key, value in metrics.items():
        print(f"  {key}: {value:.4f}")

    # Verify metrics are reasonable
    assert metrics['speedup'] > 1.0
    assert 0.95 <= metrics['quality_ratio'] <= 1.05  # Within 5%


if __name__ == '__main__':
    # Allow running benchmarks directly
    pytest.main([__file__, '-v', '-s', '-m', 'performance'])

