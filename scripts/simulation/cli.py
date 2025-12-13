"""Command-line interface for depot simulation.

Reference: PRD_v2.md Section 11.1 (MVP Acceptance Tests)
           docs/SIMULATION.md
           favonius_development_plan_v2.md Step 6.1
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.models import DepotConfig
from scripts.simulation.depot_sim import DepotSimulator, SimulationOptimizer
from scripts.simulation.scenarios import (
    morning_rush_scenario,
    price_spike_scenario,
    soc_deviation_scenario,
    demand_charge_scenario,
    realistic_depot_scenario,
)


SCENARIO_MAP = {
    "morning_rush": morning_rush_scenario,
    "price_spike": price_spike_scenario,
    "soc_deviation": soc_deviation_scenario,
    "demand_charge": demand_charge_scenario,
    "realistic": realistic_depot_scenario,
}


def get_depot_config(
    n_vehicles: int, n_chargers: int
) -> DepotConfig:
    """Create depot configuration.

    Args:
        n_vehicles: Number of vehicles
        n_chargers: Number of chargers

    Returns:
        DepotConfig instance
    """
    # Default vehicle capacities
    vehicle_capacities = {
        f"bus_{i}": 324.0 for i in range(n_vehicles)
    }

    # Per PRD Section 8.3: Chargers aggregated by rated_kw
    # Create charger groups: all chargers at 80kW
    charger_groups = {80.0: n_chargers}
    
    # Per PRD Section 6.2: DepotConfig structure
    return DepotConfig(
        vehicle_capacities=vehicle_capacities,
        vehicle_max_charge_kw={f"bus_{i}": 80.0 for i in range(n_vehicles)},
        charger_groups=charger_groups,
        charger_efficiency=0.95,
        charger_vehicle_access={},  # All vehicles can access all chargers (simple case)
        battery_capacity=1000.0,
        battery_power=200.0,
        battery_efficiency=0.92,  # Per PRD Section 8.1
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=1200.0,
        delta_t=0.25,  # 15 minutes per PRD Section 3.2
        n_timesteps=96,  # 24 hours
    )


async def run_simulation_cmd(
    scenario_name: str,
    n_vehicles: int,
    n_chargers: int,
    steps: int,
    output: Optional[str],
    verbose: bool,
) -> None:
    """Run a simulation scenario.

    Args:
        scenario_name: Name of scenario to run
        n_vehicles: Number of vehicles
        n_chargers: Number of chargers
        steps: Number of simulation steps
        output: Output file path (optional)
        verbose: Enable verbose output
    """
    if scenario_name not in SCENARIO_MAP:
        print(f"Unknown scenario: {scenario_name}")
        print(f"Available scenarios: {', '.join(SCENARIO_MAP.keys())}")
        sys.exit(1)

    scenario_func = SCENARIO_MAP[scenario_name]
    sim = scenario_func(n_vehicles=n_vehicles, n_chargers=n_chargers)

    # Create depot config
    config = get_depot_config(n_vehicles, n_chargers)

    # Create optimizer
    optimizer = SimulationOptimizer(sim, config)

    if verbose:
        print(f"Starting simulation: {scenario_name}")
        print(f"Vehicles: {n_vehicles}, Chargers: {n_chargers}")
        print(f"Steps: {steps} ({steps * 0.25:.1f} hours)")

    # Run simulation
    for step in range(steps):
        if step % 4 == 0:  # Hourly optimization
            try:
                result = await optimizer.optimize(horizon_hours=24)
                optimizer.apply_result(result)
                if verbose:
                    print(
                        f"Step {step}: Optimization completed "
                        f"(solve_time={result.solve_time_s:.2f}s, "
                        f"solver={result.solver_used})"
                    )
            except Exception as e:
                print(f"Optimization error at step {step}: {e}")
                if not verbose:
                    sys.exit(1)

        sim.step()

        if verbose and step % 4 == 0:
            state = sim.get_state()
            avg_soc = sum(v["soc"] for v in state["vehicles"]) / len(
                state["vehicles"]
            )
            print(f"  Avg SoC: {avg_soc:.2f}")

    # Print summary
    sim.print_summary()

    # Export metrics if output specified
    if output:
        output_path = Path(output)
        format = output_path.suffix[1:] if output_path.suffix else "json"
        sim.export_metrics(str(output_path), format=format)
        print(f"\nMetrics exported to: {output_path}")


async def benchmark_cmd(
    fleet_size: int,
    n_runs: int,
    output: Optional[str],
) -> None:
    """Run performance benchmarks.

    Args:
        fleet_size: Fleet size to benchmark
        n_runs: Number of benchmark runs
        output: Output file path (optional)
    """
    from src.core.models import DepotConfig, DepotState
    from src.core.optimizer import optimize

    print(f"Benchmarking {fleet_size} vehicles ({n_runs} runs)...")

    # Per PRD Section 8.3: Chargers aggregated by rated_kw
    n_chargers = max(5, fleet_size // 2)
    charger_groups = {80.0: n_chargers}
    
    # Per PRD Section 6.2: DepotConfig structure
    config = DepotConfig(
        vehicle_capacities={
            f"bus_{i}": 324.0 for i in range(fleet_size)
        },
        vehicle_max_charge_kw={
            f"bus_{i}": 80.0 for i in range(fleet_size)
        },
        charger_groups=charger_groups,
        charger_efficiency=0.95,
        charger_vehicle_access={},  # All vehicles can access all chargers
        battery_capacity=1000.0,
        battery_power=200.0,
        battery_efficiency=0.92,  # Per PRD Section 8.1
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=1200.0,
        delta_t=0.25,  # 15 minutes per PRD Section 3.2
        n_timesteps=96,  # 24 hours
    )

    n_t = config.n_timesteps
    state = DepotState(
        vehicle_socs={
            f"bus_{i}": 0.4 + i * 0.01 for i in range(fleet_size)
        },
        battery_soc=0.5,
        prices=[0.12] * n_t,
        demand_charge_rate=20.0,
        current_month_peak=400.0,
        vehicle_availability={
            f"bus_{i}": [True] * n_t for i in range(fleet_size)
        },
        energy_requirements={
            f"bus_{i}": 200.0 for i in range(fleet_size)
        },
        departure_times={
            f"bus_{i}": 24 + i % 12 for i in range(fleet_size)
        },
        building_power=[50.0] * n_t,
    )

    results = []
    for run in range(n_runs):
        # Per PRD Section 8.3: Solve time target < 60 seconds
        from src.core.optimizer.milp_model import build_optimization_model, solve_model
        model = build_optimization_model(state, config)
        result = solve_model(model, time_limit=60.0)
        
        results.append(
            {
                "run": run + 1,
                "solve_time": result.solve_time_s,  # Per PRD: solve_time_s field
                "objective_value": result.objective_value,
                "peak_demand": result.peak_demand_kw,  # Per PRD: peak_demand_kw field
                "solver_used": result.solver_used,  # Per PRD Section 8.2: Track solver
            }
        )
        print(
            f"Run {run+1}/{n_runs}: "
            f"solve_time={result.solve_time:.2f}s, "
            f"objective=${result.objective_value:.2f}"
        )

    # Calculate statistics
    solve_times = [r["solve_time"] for r in results]
    avg_time = sum(solve_times) / len(solve_times)
    min_time = min(solve_times)
    max_time = max(solve_times)

    print(f"\nBenchmark Results:")
    print(f"  Average solve time: {avg_time:.2f}s")
    print(f"  Min solve time: {min_time:.2f}s")
    print(f"  Max solve time: {max_time:.2f}s")

    if output:
        output_path = Path(output)
        with open(output_path, "w") as f:
            json.dump(
                {
                    "fleet_size": fleet_size,
                    "n_runs": n_runs,
                    "results": results,
                    "statistics": {
                        "avg_solve_time": avg_time,
                        "min_solve_time": min_time,
                        "max_solve_time": max_time,
                    },
                },
                f,
                indent=2,
            )
        print(f"\nResults exported to: {output_path}")


async def validate_cmd(
    acceptance_test: str,
    output: Optional[str],
) -> None:
    """Run acceptance test validation.

    Args:
        acceptance_test: Acceptance test name (AT-01, AT-02, etc.)
        output: Output file path (optional)
    """
    print(f"Running acceptance test: {acceptance_test}")

    # Import test functions
    from tests.integration.test_simulation_acceptance import (
        test_at01_end_to_end_simulation,
        test_at02_demand_charge_reduction,
        test_at03_price_spike_reoptimization,
        test_at04_soc_deviation_handling,
        test_at05_inter_depot_handoff,
    )

    test_map = {
        "AT-01": test_at01_end_to_end_simulation,
        "AT-02": test_at02_demand_charge_reduction,
        "AT-03": test_at03_price_spike_reoptimization,
        "AT-04": test_at04_soc_deviation_handling,
        "AT-05": test_at05_inter_depot_handoff,
    }

    if acceptance_test not in test_map:
        print(f"Unknown acceptance test: {acceptance_test}")
        print(f"Available tests: {', '.join(test_map.keys())}")
        sys.exit(1)

    test_func = test_map[acceptance_test]

    try:
        await test_func()
        print(f"\n✓ {acceptance_test} PASSED")
        result = {"test": acceptance_test, "status": "PASSED"}
    except Exception as e:
        print(f"\n✗ {acceptance_test} FAILED: {e}")
        result = {"test": acceptance_test, "status": "FAILED", "error": str(e)}
        sys.exit(1)

    if output:
        output_path = Path(output)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results exported to: {output_path}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Depot simulation CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run simulation scenario")
    run_parser.add_argument(
        "--scenario",
        required=True,
        help="Scenario name",
        choices=list(SCENARIO_MAP.keys()),
    )
    run_parser.add_argument(
        "--fleet-size",
        type=int,
        default=10,
        help="Fleet size (default: 10)",
    )
    run_parser.add_argument(
        "--chargers",
        type=int,
        default=5,
        help="Number of chargers (default: 5)",
    )
    run_parser.add_argument(
        "--steps",
        type=int,
        default=96,
        help="Number of simulation steps (default: 96 = 24 hours)",
    )
    run_parser.add_argument(
        "--output",
        help="Output file path for metrics (JSON or CSV)",
    )
    run_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    # Benchmark command
    bench_parser = subparsers.add_parser(
        "benchmark", help="Run performance benchmarks"
    )
    bench_parser.add_argument(
        "--fleet-size",
        type=int,
        default=20,
        help="Fleet size to benchmark (default: 20)",
    )
    bench_parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of benchmark runs (default: 5)",
    )
    bench_parser.add_argument(
        "--output",
        help="Output file path for results (JSON)",
    )

    # Validate command
    validate_parser = subparsers.add_parser(
        "validate", help="Run acceptance test validation"
    )
    validate_parser.add_argument(
        "--acceptance-test",
        required=True,
        help="Acceptance test name (AT-01, AT-02, etc.)",
    )
    validate_parser.add_argument(
        "--output",
        help="Output file path for results (JSON)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Run command
    if args.command == "run":
        asyncio.run(
            run_simulation_cmd(
                scenario_name=args.scenario,
                n_vehicles=args.fleet_size,
                n_chargers=args.chargers,
                steps=args.steps,
                output=args.output,
                verbose=args.verbose,
            )
        )
    elif args.command == "benchmark":
        asyncio.run(
            benchmark_cmd(
                fleet_size=args.fleet_size,
                n_runs=args.runs,
                output=args.output,
            )
        )
    elif args.command == "validate":
        asyncio.run(
            validate_cmd(
                acceptance_test=args.acceptance_test,
                output=args.output,
            )
        )


if __name__ == "__main__":
    main()

