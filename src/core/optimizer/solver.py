"""Solver wrapper and configuration for HiGHS.

Reference: PRD.md#8-2-solver-configuration
"""

import logging
from typing import TYPE_CHECKING

import pyomo.environ as pyo

from .exceptions import (
    InfeasibleModelError,
    SolverError,
    SolverTimeoutError,
)

if TYPE_CHECKING:
    from pyomo.core import ConcreteModel

logger = logging.getLogger(__name__)


def solve_model(
    model: pyo.ConcreteModel, time_limit: float = 30.0, warm_started: bool = False
) -> dict:
    """Solve the optimization model and return results.

    Reference: PRD.md#8-2-solver-configuration

    Args:
        model: Pyomo ConcreteModel to solve
        time_limit: Maximum solve time in seconds
        warm_started: Whether model has been warm-started

    Returns:
        Dictionary with:
            - schedule: dict[vehicle_id, {charging_power: list, soc: list}]
            - battery_dispatch: list of battery power per timestep
            - grid_power: list of grid power per timestep
            - peak_demand: peak demand value
            - objective_value: objective function value
            - solve_time: solve time in seconds

    Raises:
        SolverError: If solver fails
        SolverTimeoutError: If solver exceeds time limit
        InfeasibleModelError: If model is infeasible
    """
    start_type = "warm-start" if warm_started else "cold-start"
    logger.info(f"Solving model ({start_type}) with time limit {time_limit}s")

    # Configure solver per PRD Section 8.2
    solver = pyo.SolverFactory('appsi_highs')
    solver.options['time_limit'] = time_limit
    solver.options['mip_rel_gap'] = 0.01  # 1% optimality gap
    solver.options['threads'] = 4  # parallel threads
    solver.options['presolve'] = 'on'  # preprocessing

    # For warm-started models, we can slightly relax the gap since we have a good start
    if warm_started:
        # Keep same gap but solver will benefit from initial solution
        logger.debug("Using warm-started solver configuration")

    # Solve
    result = solver.solve(model, tee=False)

    # Check termination condition
    termination = result.solver.termination_condition

    if termination == pyo.TerminationCondition.optimal:
        logger.info("Solver found optimal solution")
    elif termination == pyo.TerminationCondition.maxTimeLimit:
        logger.warning(f"Solver reached time limit ({time_limit}s)")
        # Try to extract solution - if we can't, it's a timeout error
        try:
            test_value = pyo.value(model.objective)
            if test_value is None:
                raise SolverTimeoutError(time_limit)
            logger.info("Accepting feasible solution despite timeout")
        except (ValueError, TypeError):
            raise SolverTimeoutError(time_limit)
    elif termination == pyo.TerminationCondition.feasible:
        logger.info("Solver found feasible solution (not proven optimal)")
    elif termination == pyo.TerminationCondition.infeasible:
        raise InfeasibleModelError("Model is infeasible")
    elif termination == pyo.TerminationCondition.unbounded:
        raise SolverError("Model is unbounded", str(termination))
    else:
        raise SolverError(
            f"Solver failed with termination condition: {termination}",
            str(termination),
        )

    # Extract solution
    solve_time = getattr(result.solver, 'time', 0.0)

    schedule = {}
    for b in model.B:
        schedule[str(b)] = {
            'charging_power': [
                pyo.value(model.P_charge[b, t]) for t in model.T
            ],
            'soc': [pyo.value(model.SoC[b, t]) for t in model.T],
        }

    battery_dispatch = [pyo.value(model.P_batt[t]) for t in model.T]
    grid_power = [pyo.value(model.P_grid[t]) for t in model.T]
    peak_demand = pyo.value(model.P_peak)
    objective_value = pyo.value(model.objective)

    logger.info(
        f"Solution extracted: objective=${objective_value:.2f}, "
        f"peak={peak_demand:.2f}kW, solve_time={solve_time:.2f}s"
    )

    return {
        'schedule': schedule,
        'battery_dispatch': battery_dispatch,
        'grid_power': grid_power,
        'peak_demand': peak_demand,
        'objective_value': objective_value,
        'solve_time': solve_time,
    }

