"""Solver wrapper and configuration for Gurobi with HiGHS fallback.

Reference: PRD_v2.md#8-2-solver-configuration
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
    model: pyo.ConcreteModel, time_limit: float = 60.0, warm_started: bool = False
) -> dict:
    """Solve the optimization model and return results.

    Implements Gurobi primary with automatic HiGHS fallback per PRD Section 8.2.
    Reference: PRD_v2.md#8-2-solver-configuration

    Args:
        model: Pyomo ConcreteModel to solve
        time_limit: Maximum solve time in seconds (default: 60 per PRD)
        warm_started: Whether model has been warm-started

    Returns:
        result_dict containing:
            - schedule: dict[vehicle_id, {charging_power: list, soc: list}]
            - battery_dispatch: list of battery power per timestep
            - grid_power: list of grid power per timestep
            - peak_demand_kw: peak demand value (kW)
            - objective_value: objective function value
            - solve_time_s: solve time in seconds
            - solve_time: solve time in seconds (legacy alias)
            - solver_used: 'gurobi' or 'highs' indicating which solver was used

    Raises:
        SolverError: If both solvers fail
        SolverTimeoutError: If solver exceeds time limit
        InfeasibleModelError: If model is infeasible
    """
    start_type = "warm-start" if warm_started else "cold-start"
    logger.info(f"Solving model ({start_type}) with time limit {time_limit}s")

    # Primary solver: Gurobi (per PRD Section 8.2)
    solver_used = 'gurobi'
    try:
        solver = pyo.SolverFactory('gurobi')
        if solver is None or not solver.available():
            raise SolverError("Gurobi solver not available", "license_check")
        
        # Configure Gurobi per PRD Section 8.2
        solver.options['TimeLimit'] = time_limit
        solver.options['MIPGap'] = 0.01  # 1% optimality gap
        solver.options['Threads'] = 4
        solver.options['Presolve'] = 2  # Aggressive presolve
        solver.options['NumericFocus'] = 3  # Highest numerical accuracy
        solver.options['OutputFlag'] = 1
        
        if warm_started:
            solver.options['WarmStart'] = 1
        
        logger.info("Attempting solve with Gurobi solver")
        result = solver.solve(model, tee=False)
        
    except Exception as e:
        # Fallback to HiGHS if Gurobi fails
        logger.warning(f"Gurobi solver failed: {e}. Falling back to HiGHS.")
        solver_used = 'highs'
        
        try:
            fallback_solver = pyo.SolverFactory('appsi_highs')
            if fallback_solver is None or not fallback_solver.available():
                raise SolverError("HiGHS solver not available", "solver_unavailable")
            
            # Configure HiGHS per PRD Section 8.2
            fallback_solver.options['time_limit'] = time_limit
            fallback_solver.options['mip_rel_gap'] = 0.01  # Same 1% gap target
            fallback_solver.options['threads'] = 4
            fallback_solver.options['presolve'] = 'on'
            
            logger.info("Attempting solve with HiGHS fallback solver")
            result = fallback_solver.solve(model, tee=False)
            
        except Exception as fallback_error:
            logger.error(f"Both Gurobi and HiGHS solvers failed. Gurobi: {e}, HiGHS: {fallback_error}")
            raise SolverError(
                f"Both solvers failed. Gurobi: {str(e)}, HiGHS: {str(fallback_error)}",
                "solver_failure"
            )

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
    solve_time_s = getattr(result.solver, 'time', 0.0)

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
    peak_demand_kw = pyo.value(model.P_peak)
    objective_value = pyo.value(model.objective)

    logger.info(
        f"Solution extracted ({solver_used}): objective=${objective_value:.2f}, "
        f"peak={peak_demand_kw:.2f}kW, solve_time={solve_time_s:.2f}s"
    )

    result_dict = {
        'schedule': schedule,
        'battery_dispatch': battery_dispatch,
        'grid_power': grid_power,
        'peak_demand_kw': peak_demand_kw,
        'peak_demand': peak_demand_kw,
        'objective_value': objective_value,
        'solve_time_s': solve_time_s,
        'solve_time': solve_time_s,
        'solver_used': solver_used,
    }
    
    return result_dict

