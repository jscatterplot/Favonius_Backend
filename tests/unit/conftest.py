import sys
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

# Pyomo is an optional heavy dependency not installed in the unit-test
# environment. Stub it out before any src.* imports trigger the chain:
#   src.api.main → src.core.controller_manager → src.core.optimizer → pyomo
if "pyomo" not in sys.modules:
    _pyomo_mock = MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from unittest.mock import patch as _patch

from src.core.models import DepotConfig, DepotState, OptimizationResult


def pytest_configure(config):
    """Clear Prometheus registry before any tests are collected."""
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)


@pytest.fixture(autouse=True, scope="session")
def clear_prometheus_registry_session():
    """Clear the Prometheus default registry at session start to avoid conflicts."""
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)

    yield

    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)


@pytest.fixture(autouse=True)
def clear_prometheus_registry():
    """Clear the Prometheus default registry before each test to avoid conflicts."""
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)

    yield

    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)


# ── Geo-block bypass ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def bypass_geo_block():
    """Disable geo-blocking for all unit tests.

    GeoIP is unavailable in the test environment so the middleware runs in
    fail-closed mode and blocks every testclient request before auth can fire.
    Patch check_ip_blocked to always return non-blocked.
    """
    _not_blocked = MagicMock(blocked=False)
    with _patch("src.security.geo_block.check_ip_blocked", return_value=_not_blocked):
        yield


# ── Shared test fixtures ────────────────────────────────────────────────────


@pytest.fixture
def sample_depot_id() -> str:
    """A stable depot UUID string for use across fixtures."""
    return str(uuid4())


@pytest.fixture
def client():
    """FastAPI TestClient for the main app."""
    from src.api.main import app

    return TestClient(app)


@pytest.fixture
def mock_db_pool():
    """Mock database connection pool.

    Returns a (pool, conn) tuple where pool.ts and pool.static both delegate
    to pool.acquire so tests that patch db_pools with this pool work correctly.
    """
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool, conn


@pytest.fixture
def sample_depot_config():
    """Sample depot configuration for testing."""
    vehicle_ids = ["bus_1", "bus_2"]
    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 10},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


@pytest.fixture
def sample_depot_state():
    """Sample depot state for testing."""
    return DepotState(
        vehicle_socs={"bus_1": 0.45, "bus_2": 0.82},
        battery_soc=0.55,
        prices=[0.10, 0.15, 0.12] * 32,  # 96 timesteps
        demand_charge_rate=20.0,
        current_month_peak=380.0,
        vehicle_availability={
            "bus_1": [True] * 96,
            "bus_2": [True] * 96,
        },
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        building_power=[50.0] * 96,
    )


@pytest.fixture
def sample_optimization_result(sample_depot_config):
    """Sample optimization result for testing."""
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            "bus_1": {
                "charging_power": [0, 0, 80, 80] * 24,
                "soc": [0.3, 0.3, 0.35, 0.40] * 24,
            },
            "bus_2": {
                "charging_power": [80, 80, 0, 0] * 24,
                "soc": [0.5, 0.55, 0.55, 0.55] * 24,
            },
        },
        battery_dispatch=[0.0] * 96,
        grid_power=[100.0] * 96,
        peak_demand=450.0,
        objective_value=1234.56,
        solve_time=12.3,
        status="completed",
    )


# ── User token fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def admin_user():
    """JWT payload for a user with admin role."""
    return {"sub": str(uuid4()), "user_metadata": {"favonius_role": "admin"}}


@pytest.fixture
def operator_user():
    """JWT payload for a user with operator role."""
    return {"sub": str(uuid4()), "user_metadata": {"favonius_role": "operator"}}


@pytest.fixture
def viewer_user():
    """JWT payload for a user with viewer role."""
    return {"sub": str(uuid4()), "user_metadata": {"favonius_role": "viewer"}}


@pytest.fixture
def auditor_user():
    """JWT payload for a user with auditor role."""
    return {"sub": str(uuid4()), "user_metadata": {"favonius_role": "auditor"}}


@pytest.fixture
def user_with_depot_ids(sample_depot_id):
    """JWT payload for a user whose access comes from the depot_ids JWT claim."""
    return {"sub": str(uuid4()), "user_metadata": {"depot_ids": [sample_depot_id]}}
