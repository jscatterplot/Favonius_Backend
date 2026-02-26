"""Unit tests for optimizer exceptions.

Reference: PRD Section 11.2
"""

from src.core.optimizer import (
    ConstraintViolationError,
    InfeasibleModelError,
    InvalidConfigError,
    InvalidStateError,
    OptimizationError,
    SolverError,
    SolverTimeoutError,
)


def test_optimization_error_base():
    """Test base OptimizationError."""
    error = OptimizationError("Test error")
    assert "Test error" in str(error)


def test_infeasible_model_error():
    """Test InfeasibleModelError."""
    error = InfeasibleModelError("Model is infeasible")
    assert "infeasible" in str(error).lower()
    assert error.message == "Model is infeasible"


def test_solver_timeout_error():
    """Test SolverTimeoutError."""
    error = SolverTimeoutError(30.0)
    assert error.time_limit == 30.0
    assert "30" in str(error)
    assert error.message is not None

    # Test with custom message
    error2 = SolverTimeoutError(60.0, "Custom timeout message")
    assert error2.time_limit == 60.0
    assert "Custom timeout message" in str(error2)


def test_solver_error():
    """Test SolverError."""
    error = SolverError("Solver failed", "error_status")
    assert "Solver failed" in str(error)
    assert error.message == "Solver failed"
    assert error.solver_status == "error_status"

    # Test without solver_status
    error2 = SolverError("Solver failed")
    assert error2.solver_status is None


def test_invalid_state_error():
    """Test InvalidStateError."""
    error = InvalidStateError("Invalid state", "vehicle_socs")
    assert "Invalid state" in str(error)
    assert error.message == "Invalid state"
    assert error.field == "vehicle_socs"

    # Test without field
    error2 = InvalidStateError("Invalid state")
    assert error2.field is None


def test_invalid_config_error():
    """Test InvalidConfigError."""
    error = InvalidConfigError("Invalid config", "charger_power")
    assert "Invalid config" in str(error)
    assert error.message == "Invalid config"
    assert error.field == "charger_power"

    # Test without field
    error2 = InvalidConfigError("Invalid config")
    assert error2.field is None


def test_constraint_violation_error():
    """Test ConstraintViolationError."""
    error = ConstraintViolationError("Constraint violated", "departure_soc", "bus_1")
    assert "Constraint violated" in str(error)
    assert error.message == "Constraint violated"
    assert error.constraint_name == "departure_soc"
    assert error.vehicle_id == "bus_1"

    # Test without vehicle_id
    error2 = ConstraintViolationError("Constraint violated", "departure_soc")
    assert error2.vehicle_id is None
