"""Regression tests: no duplicate Prometheus metric registrations on startup.

The crash:
    ValueError: Duplicated timeseries in CollectorRegistry:
        {'favonius_optimization_runs_created', 'favonius_optimization_runs',
         'favonius_optimization_runs_total'}

Root cause: src/core/controller.py declared seven Prometheus metrics at module
level using the same names as src/monitoring/metrics.py.  When api/main.py
imported ControllerManager (line 26) the controller metrics were registered
first; then the import of monitoring.metrics (line 30) tried to register the
same names again, raising ValueError and crashing the API before it could bind
a port.

These tests catch the regression at import time so the bug is caught in CI
before it reaches the container.
"""

from __future__ import annotations

import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _fresh_registry():
    """Return a new CollectorRegistry so tests don't pollute each other."""
    from prometheus_client import CollectorRegistry

    return CollectorRegistry()


# ---------------------------------------------------------------------------
# 1. monitoring/metrics.py defines the canonical names exactly once
# ---------------------------------------------------------------------------


class TestMonitoringMetricsModule:
    # prometheus_client stores Counter names without the _total suffix internally.
    # Use the base names (without _total) for the _name attribute check.
    EXPECTED_BASE_NAMES = {
        "favonius_optimization_runs",
        "favonius_optimization_duration_seconds",
        "favonius_optimization_failures",
        "favonius_ocpp_dispatch_success",
        "favonius_ocpp_dispatch_failures",
        "favonius_control_loop_uptime_seconds",
        "favonius_controller_state",
        "favonius_controller_manager_up",
        # Additional metrics only in monitoring/metrics.py
        "favonius_optimization_objective_value",
        "favonius_solver_used",
        "favonius_solver_fallback",
        "favonius_vehicle_soc",
        "favonius_grid_power_kw",
        "favonius_peak_demand_kw",
    }

    def test_all_expected_metrics_present(self) -> None:
        """All favonius_* metric base names must be reachable from monitoring.metrics."""
        from src.monitoring import metrics as m

        exported = [
            name
            for name in dir(m)
            if not name.startswith("_") and hasattr(getattr(m, name), "_name")
        ]
        metric_names = {getattr(m, n)._name for n in exported}

        for expected in self.EXPECTED_BASE_NAMES:
            assert expected in metric_names, (
                f"Expected metric base name '{expected}' not found in src/monitoring/metrics.py. "
                "If you moved or renamed it, update this test."
            )

    def test_controller_does_not_declare_its_own_metrics(self) -> None:
        """controller.py must NOT create Counter/Gauge/Histogram at module level.

        All metrics must be imported from src.monitoring.metrics so there is
        a single registration per process lifetime.
        """
        import ast
        import pathlib

        controller_src = pathlib.Path("src/core/controller.py").read_text()
        tree = ast.parse(controller_src)

        prometheus_constructors = {"Counter", "Gauge", "Histogram", "Summary", "Info"}

        for node in ast.walk(tree):
            # Module-level assignments that call a Prometheus constructor
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in prometheus_constructors:
                    raise AssertionError(
                        f"controller.py creates a Prometheus metric directly "
                        f"('{name}(...)'). Import it from src.monitoring.metrics "
                        f"instead to avoid duplicate registration on startup."
                    )


# ---------------------------------------------------------------------------
# 2. Import order: ControllerManager then monitoring.metrics must not crash
# ---------------------------------------------------------------------------


class TestImportOrderDoesNotCrash:
    """Simulate the exact import sequence that api/main.py performs."""

    def test_controller_manager_then_monitoring_metrics(self) -> None:
        """Importing ControllerManager before monitoring.metrics must not raise.

        This reproduces the crash:
            api/main.py line 26: from ..core.controller_manager import ControllerManager
            api/main.py line 30: from ..monitoring.metrics import CONTROLLER_MANAGER_UP
        If controller.py declares its own metrics, the second import raises
        ValueError: Duplicated timeseries.

        Requires pyomo (MILP solver library) — skipped in minimal environments.
        """
        pytest.importorskip("pyomo", reason="pyomo not installed in this environment")
        try:
            importlib.import_module("src.core.controller_manager")
            importlib.import_module("src.monitoring.metrics")
        except ValueError as exc:
            raise AssertionError(
                f"Duplicate Prometheus metric registration detected: {exc}\n"
                "src/core/controller.py is probably declaring its own metrics "
                "instead of importing from src.monitoring.metrics."
            ) from exc

    def test_monitoring_metrics_then_controller_manager(self) -> None:
        """Reverse order: monitoring.metrics first, then ControllerManager.

        Requires pyomo — skipped in minimal environments.
        """
        pytest.importorskip("pyomo", reason="pyomo not installed in this environment")
        try:
            importlib.import_module("src.monitoring.metrics")
            importlib.import_module("src.core.controller_manager")
        except ValueError as exc:
            raise AssertionError(
                f"Duplicate Prometheus metric registration detected: {exc}"
            ) from exc

    def test_controller_manager_up_importable_from_monitoring(self) -> None:
        """CONTROLLER_MANAGER_UP must be in monitoring.metrics (used by api/main.py)."""
        from src.monitoring.metrics import CONTROLLER_MANAGER_UP  # noqa: F401

        assert CONTROLLER_MANAGER_UP is not None
