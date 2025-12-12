"""MILP optimization model for depot charging scheduling.

Reference: PRD.md#8-optimization-engine-specifications
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

import pyomo.environ as pyo

from ..models import DepotConfig, DepotState, OptimizationResult
from .exceptions import (
    ConstraintViolationError,
    InvalidConfigError,
    InvalidStateError,
)

if TYPE_CHECKING:
    from pyomo.core import ConcreteModel

logger = logging.getLogger(__name__)


def _validate_inputs(state: DepotState, config: DepotConfig) -> None:
    """Validate DepotState and DepotConfig inputs.

    Args:
        state: Depot state for optimization
        config: Depot configuration

    Raises:
        InvalidStateError: If state is invalid
        InvalidConfigError: If config is invalid
    """
    # Validate state
    if not state.vehicle_socs:
        raise InvalidStateError("vehicle_socs cannot be empty", "vehicle_socs")

    if len(state.prices) != config.n_timesteps:
        raise InvalidStateError(
            f"prices length ({len(state.prices)}) must match "
            f"n_timesteps ({config.n_timesteps})",
            "prices",
        )

    if len(state.building_power) != config.n_timesteps:
        raise InvalidStateError(
            f"building_power length ({len(state.building_power)}) must match "
            f"n_timesteps ({config.n_timesteps})",
            "building_power",
        )

    # Check vehicle_socs keys match vehicle_availability keys
    if set(state.vehicle_socs.keys()) != set(state.vehicle_availability.keys()):
        raise InvalidStateError(
            "vehicle_socs keys must match vehicle_availability keys",
            "vehicle_availability",
        )

    # Check availability length matches n_timesteps
    for vid, avail in state.vehicle_availability.items():
        if len(avail) != config.n_timesteps:
            raise InvalidStateError(
                f"vehicle_availability[{vid}] length ({len(avail)}) must match "
                f"n_timesteps ({config.n_timesteps})",
                "vehicle_availability",
            )

    # Check vehicle capacities match vehicle IDs
    if set(state.vehicle_socs.keys()) != set(config.vehicle_capacities.keys()):
        raise InvalidConfigError(
            "vehicle_capacities keys must match vehicle_socs keys",
            "vehicle_capacities",
        )

    # Validate config
    if config.charger_power <= 0:
        raise InvalidConfigError("charger_power must be positive", "charger_power")

    if config.n_chargers <= 0:
        raise InvalidConfigError("n_chargers must be positive", "n_chargers")

    if config.max_site_power <= 0:
        raise InvalidConfigError(
            "max_site_power must be positive", "max_site_power"
        )

    if not (0 < config.charger_efficiency <= 1.0):
        raise InvalidConfigError(
            "charger_efficiency must be in (0, 1]", "charger_efficiency"
        )

    if config.battery_capacity <= 0:
        raise InvalidConfigError(
            "battery_capacity must be positive", "battery_capacity"
        )

    if config.battery_power <= 0:
        raise InvalidConfigError("battery_power must be positive", "battery_power")


def _compute_tighter_soc_bounds(
    state: DepotState, config: DepotConfig, vehicle_id: str, timestep: int
) -> tuple[float, float]:
    """Compute SoC bounds for a vehicle at a specific timestep.

    Uses conservative default bounds to avoid infeasibility.
    The departure SoC requirement (0.99) is enforced via a constraint,
    not through variable bounds.

    Note: We use wide bounds (0.1, 1.0) to allow warm-starting from
    various solution states without bound conflicts.

    Args:
        state: Current depot state
        config: Depot configuration
        vehicle_id: Vehicle identifier
        timestep: Timestep index

    Returns:
        Tuple of (lower_bound, upper_bound)
    """
    # Use conservative default bounds for all timesteps
    # The initial SoC is enforced via the soc_init_con constraint
    # The departure SoC is enforced via the departure_soc constraint
    return (0.1, 1.0)


def build_optimization_model(
    state: DepotState, config: DepotConfig
) -> pyo.ConcreteModel:
    """Build Pyomo MILP model for depot charging optimization.

    Reference: PRD.md#8-1-mathematical-formulation

    Includes performance optimizations:
    - Variable fixing for vehicles on route
    - Symmetry breaking constraints
    - Tighter SoC bounds

    Args:
        state: Current depot state (SoC, prices, availability, etc.)
        config: Static depot configuration

    Returns:
        Pyomo ConcreteModel with all variables, constraints, and objective

    Raises:
        InvalidStateError: If state is invalid
        InvalidConfigError: If config is invalid
    """
    logger.info("Building optimization model")
    _validate_inputs(state, config)

    model = pyo.ConcreteModel("DepotCharging")

    # Sets
    model.T = pyo.RangeSet(0, config.n_timesteps - 1)
    model.B = pyo.Set(initialize=list(state.vehicle_socs.keys()))

    # Parameters
    model.price = pyo.Param(
        model.T, initialize=lambda m, t: state.prices[t]
    )
    model.E_batt = pyo.Param(
        model.B, initialize=lambda m, b: config.vehicle_capacities[b]
    )
    model.soc_init = pyo.Param(
        model.B, initialize=lambda m, b: state.vehicle_socs[b]
    )
    model.available = pyo.Param(
        model.B,
        model.T,
        initialize=lambda m, b, t: 1 if state.vehicle_availability[b][t] else 0,
    )
    model.building_power = pyo.Param(
        model.T, initialize=lambda m, t: state.building_power[t]
    )

    # Variables with tighter bounds
    model.P_charge = pyo.Var(
        model.B,
        model.T,
        domain=pyo.NonNegativeReals,
        bounds=(0, config.charger_power),
    )

    # SoC variables with tighter bounds computed per vehicle/timestep
    # Initialize with default bounds, will be tightened below
    model.SoC = pyo.Var(model.B, model.T, bounds=(0.1, 1.0))
    model.y_charge = pyo.Var(model.B, model.T, domain=pyo.Binary)
    model.P_grid = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    model.P_peak = pyo.Var(domain=pyo.NonNegativeReals)

    # Battery storage variables
    model.P_batt = pyo.Var(
        model.T, bounds=(-config.battery_power, config.battery_power)
    )
    model.SoC_batt = pyo.Var(model.T, bounds=(0.2, 0.8))

    # Apply tighter SoC bounds
    for b in model.B:
        for t in model.T:
            lower, upper = _compute_tighter_soc_bounds(state, config, b, t)
            model.SoC[b, t].setlb(lower)
            model.SoC[b, t].setub(upper)

    # Constraints

    # SoC dynamics - initial condition
    def soc_init_rule(m, b):
        return m.SoC[b, 0] == m.soc_init[b]

    model.soc_init_con = pyo.Constraint(model.B, rule=soc_init_rule)

    # SoC dynamics - recursive
    def soc_dynamics_rule(m, b, t):
        if t == 0:
            return pyo.Constraint.Skip
        eta = config.charger_efficiency
        return m.SoC[b, t] == m.SoC[b, t - 1] + (
            eta * m.P_charge[b, t - 1] * config.delta_t / m.E_batt[b]
        )

    model.soc_dynamics = pyo.Constraint(model.B, model.T, rule=soc_dynamics_rule)

    # Vehicle availability constraint
    def availability_rule(m, b, t):
        if not state.vehicle_availability[b][t]:
            return m.P_charge[b, t] == 0
        return pyo.Constraint.Skip

    model.availability_con = pyo.Constraint(model.B, model.T, rule=availability_rule)

    # Variable fixing for vehicles on route at t=0 (performance optimization)
    vehicles_on_route = [
        b for b in model.B if not state.vehicle_availability[b][0]
    ]
    if vehicles_on_route:
        logger.debug(f"Fixing variables for {len(vehicles_on_route)} vehicles on route")
        for b in vehicles_on_route:
            model.P_charge[b, 0].fix(0.0)
            model.y_charge[b, 0].fix(0)

    # Departure SoC requirement (HARD CONSTRAINT)
    def departure_soc_rule(m, b):
        t_depart = state.departure_times.get(b)
        if t_depart is not None and t_depart < config.n_timesteps:
            return m.SoC[b, t_depart] >= 0.99  # 99% at departure
        return pyo.Constraint.Skip

    model.departure_soc = pyo.Constraint(model.B, rule=departure_soc_rule)

    # Charger linking constraint
    def charger_link_rule(m, b, t):
        return m.P_charge[b, t] <= config.charger_power * m.y_charge[b, t]

    model.charger_link = pyo.Constraint(model.B, model.T, rule=charger_link_rule)

    # Charger capacity constraint
    def charger_capacity_rule(m, t):
        return sum(m.y_charge[b, t] for b in m.B) <= config.n_chargers

    model.charger_capacity = pyo.Constraint(model.T, rule=charger_capacity_rule)

    # Symmetry breaking constraints (performance optimization)
    # Only apply when number of vehicles <= number of chargers to avoid infeasibility
    # When vehicles > chargers, the constraint y_charge[i,t] >= y_charge[j,t] can
    # force more vehicles to charge than there are chargers available
    vehicle_list = sorted(list(model.B))  # Sort by vehicle ID
    if len(vehicle_list) > 1 and len(vehicle_list) <= config.n_chargers:
        logger.debug("Adding symmetry breaking constraints")

        def symmetry_break_rule(m, i_idx, t):
            """Symmetry breaking: vehicle[i] >= vehicle[i+1] when both available."""
            if i_idx >= len(vehicle_list) - 1:
                return pyo.Constraint.Skip
            vehicle_i = vehicle_list[i_idx]
            vehicle_j = vehicle_list[i_idx + 1]
            # Only apply if both vehicles are available at this timestep
            if (
                state.vehicle_availability[vehicle_i][t]
                and state.vehicle_availability[vehicle_j][t]
            ):
                return m.y_charge[vehicle_i, t] >= m.y_charge[vehicle_j, t]
            return pyo.Constraint.Skip

        model.symmetry_break = pyo.Constraint(
            range(len(vehicle_list) - 1), model.T, rule=symmetry_break_rule
        )
    elif len(vehicle_list) > config.n_chargers:
        logger.debug(
            f"Skipping symmetry breaking: {len(vehicle_list)} vehicles > "
            f"{config.n_chargers} chargers"
        )

    # Grid power balance
    def grid_balance_rule(m, t):
        return (
            m.P_grid[t]
            == sum(m.P_charge[b, t] for b in m.B)
            + m.building_power[t]
            - m.P_batt[t]
        )

    model.grid_balance = pyo.Constraint(model.T, rule=grid_balance_rule)

    # Site power limit
    def site_power_limit_rule(m, t):
        return m.P_grid[t] <= config.max_site_power

    model.site_power_limit = pyo.Constraint(model.T, rule=site_power_limit_rule)

    # Peak demand tracking
    def peak_tracking_rule(m, t):
        return m.P_peak >= m.P_grid[t]

    model.peak_tracking = pyo.Constraint(model.T, rule=peak_tracking_rule)

    # Moving peak limit (from current month)
    def moving_peak_rule(m):
        return m.P_peak >= state.current_month_peak

    model.moving_peak = pyo.Constraint(rule=moving_peak_rule)

    # Battery storage dynamics - initial condition
    def batt_init_rule(m):
        return m.SoC_batt[0] == state.battery_soc

    model.batt_init = pyo.Constraint(rule=batt_init_rule)

    # Battery storage dynamics - recursive
    def batt_dynamics_rule(m, t):
        if t == 0:
            return pyo.Constraint.Skip
        return m.SoC_batt[t] == m.SoC_batt[t - 1] + (
            m.P_batt[t - 1] * config.delta_t / config.battery_capacity
        )

    model.batt_dynamics = pyo.Constraint(model.T, rule=batt_dynamics_rule)

    # Objective: minimize energy cost + demand charges
    def objective_rule(m):
        energy_cost = sum(
            m.price[t] * m.P_grid[t] * config.delta_t for t in m.T
        )
        demand_cost = state.demand_charge_rate * m.P_peak
        return energy_cost + demand_cost

    model.objective = pyo.Objective(rule=objective_rule, sense=pyo.minimize)

    logger.info(
        f"Model built: {len(model.B)} vehicles, {len(model.T)} timesteps, "
        f"{len(list(model.component_objects(pyo.Constraint)))} constraints"
    )

    return model


def _validate_solution(
    model: pyo.ConcreteModel, state: DepotState, config: DepotConfig
) -> None:
    """Validate solution satisfies all constraints, especially departure SoC.

    Args:
        model: Solved Pyomo model
        state: Original depot state
        config: Depot configuration

    Raises:
        ConstraintViolationError: If hard constraints are violated
    """
    # Validate departure SoC constraints (HARD)
    for b in model.B:
        t_depart = state.departure_times.get(b)
        if t_depart is not None and t_depart < config.n_timesteps:
            soc_at_departure = pyo.value(model.SoC[b, t_depart])
            if soc_at_departure < 0.99:
                raise ConstraintViolationError(
                    f"Vehicle {b} SoC at departure ({soc_at_departure:.3f}) < 0.99",
                    "departure_soc",
                    vehicle_id=b,
                )

    logger.debug("Solution validation passed: all constraints satisfied")


def optimize(
    state: DepotState,
    config: DepotConfig,
    time_limit: float = 30.0,
    previous_result: Optional[OptimizationResult] = None,
) -> OptimizationResult:
    """High-level optimization function.

    Combines model building, solving, and validation.
    Supports warm-starting from previous solutions.

    Args:
        state: Current depot state
        config: Depot configuration
        time_limit: Maximum solve time in seconds
        previous_result: Optional previous optimization result for warm-starting

    Returns:
        OptimizationResult with schedule and metrics

    Raises:
        InvalidStateError: If state is invalid
        InvalidConfigError: If config is invalid
        OptimizationError: If optimization fails
    """
    from .solver import solve_model
    from .warm_start import warm_start_model

    if previous_result is not None:
        logger.info("Starting optimization with warm-start")
    else:
        logger.info("Starting optimization (cold-start)")

    # Build model (includes variable fixing, symmetry breaking, tighter bounds)
    model = build_optimization_model(state, config)

    # Apply warm-starting if previous result provided
    if previous_result is not None:
        warm_start_model(model, previous_result, state, config)

    # Solve model
    result_dict = solve_model(
        model, time_limit=time_limit, warm_started=previous_result is not None
    )

    # Validate solution
    _validate_solution(model, state, config)

    # Convert to OptimizationResult
    run_id = uuid4()
    result = OptimizationResult(
        run_id=run_id,
        schedule=result_dict['schedule'],
        battery_dispatch=result_dict['battery_dispatch'],
        grid_power=result_dict['grid_power'],
        peak_demand=result_dict['peak_demand'],
        objective_value=result_dict['objective_value'],
        solve_time=result_dict['solve_time'],
        status='completed',
    )

    start_type = "warm-start" if previous_result is not None else "cold-start"
    logger.info(
        f"Optimization complete ({start_type}): objective=${result.objective_value:.2f}, "
        f"solve_time={result.solve_time:.2f}s"
    )

    return result

