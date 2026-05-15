"""MILP optimization engine for depot charging scheduling.

Reference: PRD.md#8-optimization-engine-specifications
"""

from ..models import DepotConfig, DepotState, OptimizationResult
from .allocator import ChargerAssignment, allocate_chargers
from .exceptions import (
    ConstraintViolationError,
    InfeasibleModelError,
    InvalidConfigError,
    InvalidStateError,
    OptimizationError,
    SolverError,
    SolverTimeoutError,
)
from .milp_model import build_optimization_model, optimize
from .pool import SolverPool, get_solver_pool, set_solver_pool
from .solver import solve_model
from .warm_start import warm_start_model

__all__ = [
    # Functions
    "build_optimization_model",
    "solve_model",
    "optimize",
    "warm_start_model",
    "allocate_chargers",
    "ChargerAssignment",
    # Process pool
    "SolverPool",
    "get_solver_pool",
    "set_solver_pool",
    # Data classes
    "DepotState",
    "DepotConfig",
    "OptimizationResult",
    # Exceptions
    "OptimizationError",
    "InfeasibleModelError",
    "SolverTimeoutError",
    "SolverError",
    "InvalidStateError",
    "InvalidConfigError",
    "ConstraintViolationError",
]
