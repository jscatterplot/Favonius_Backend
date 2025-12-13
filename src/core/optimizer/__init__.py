"""MILP optimization engine for depot charging scheduling.

Reference: PRD.md#8-optimization-engine-specifications
"""

from ..models import DepotConfig, DepotState, OptimizationResult
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
from .solver import solve_model
from .warm_start import warm_start_model
from .allocator import allocate_chargers, ChargerAssignment

__all__ = [
    'build_optimization_model',
    'optimize',
    'solve_model',
    'warm_start_model',
    'allocate_chargers',
    'ChargerAssignment',
]

__all__ = [
    # Functions
    'build_optimization_model',
    'solve_model',
    'optimize',
    'warm_start_model',
    # Data classes
    'DepotState',
    'DepotConfig',
    'OptimizationResult',
    # Exceptions
    'OptimizationError',
    'InfeasibleModelError',
    'SolverTimeoutError',
    'SolverError',
    'InvalidStateError',
    'InvalidConfigError',
    'ConstraintViolationError',
]
