"""Custom exceptions for optimization engine.

Reference: PRD.md#8-optimization-engine-specifications
"""

from typing import Optional


class OptimizationError(Exception):
    """Base exception for optimization errors."""

    pass


class InfeasibleModelError(OptimizationError):
    """Raised when the optimization model is infeasible."""

    def __init__(self, message: str = "Optimization model is infeasible"):
        super().__init__(message)
        self.message = message


class SolverTimeoutError(OptimizationError):
    """Raised when solver exceeds time limit."""

    def __init__(self, time_limit: float, message: Optional[str] = None):
        if message is None:
            message = f"Solver exceeded time limit of {time_limit} seconds"
        super().__init__(message)
        self.time_limit = time_limit
        self.message = message


class SolverError(OptimizationError):
    """Raised when solver encounters an error."""

    def __init__(self, message: str, solver_status: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.solver_status = solver_status


class RuntimeError(SolverError):
    """Backward-compatible runtime exception alias for optimizer errors."""


class InvalidStateError(OptimizationError):
    """Raised when DepotState is invalid."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


class InvalidConfigError(OptimizationError):
    """Raised when DepotConfig is invalid."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


class ConstraintViolationError(OptimizationError):
    """Raised when solution violates hard constraints."""

    def __init__(self, message: str, constraint_name: str, vehicle_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.constraint_name = constraint_name
        self.vehicle_id = vehicle_id
