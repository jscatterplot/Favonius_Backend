"""Core optimization and state management modules."""

from .controller import DepotController
from .models import (
    Depot,
    DepotConfig,
    DepotState,
    OptimizationResult,
    Vehicle,
)
from .optimizer import (
    build_optimization_model,
    optimize,
    solve_model,
)
from .state import StateAssembler, TriggerConfig, TriggerMonitor

__all__ = [
    # Controller
    "DepotController",
    # Models
    "Depot",
    "DepotConfig",
    "DepotState",
    "OptimizationResult",
    "Vehicle",
    # Optimizer
    "build_optimization_model",
    "optimize",
    "solve_model",
    # State
    "StateAssembler",
    "TriggerMonitor",
    "TriggerConfig",
]
