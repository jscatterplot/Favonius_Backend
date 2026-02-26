"""Integration tests for GET /depots/{depot_id}/alerts (PRD §7.1, AT-16)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

try:
    from fastapi.testclient import TestClient

    HAS_HTTPX = True
except ImportError:
    TestClient = None
    HAS_HTTPX = False

from src.api.main import app


@pytest.fixture
def client():
    if not HAS_HTTPX:
        pytest.skip("httpx required for TestClient")
    return TestClient(app)


@pytest.fixture
def depot_id():
    return str(uuid4())


@pytest.fixture
def mock_db_pool():
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


class TestAlertsAT16:
    """AT-16: Alerts and Observability acceptance tests."""

    @patch("src.api.main.db_pool")
    @patch("src.api.main.verify_token")
    def test_alerts_returns_last_optimization_and_charger_faults(
        self, mock_verify, mock_pool, client, mock_db_pool, depot_id
    ):
        """GIVEN a depot WITH at least one charger WHEN GET /depots/{id}/alerts THEN last_optimization and charger_faults present."""
        mock_verify.return_value = {"sub": "test"}
        pool, conn = mock_db_pool
        run_id = uuid4()
        now = datetime.utcnow()

        conn.fetchval = AsyncMock(return_value=1)
        conn.fetchrow = AsyncMock(
            return_value={
                "run_id": run_id,
                "run_time": now,
                "status": "optimal",
                "solver_used": "gurobi",
                "solve_time_s": 8.5,
            }
        )
        conn.fetch = AsyncMock(return_value=[])

        with patch("src.api.main.db_pool", pool):
            response = client.get(
                f"/depots/{depot_id}/alerts",
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "last_optimization" in data
        assert data["last_optimization"] is not None
        assert data["last_optimization"]["run_id"] == str(run_id)
        assert data["last_optimization"]["status"] == "optimal"
        assert data["last_optimization"]["solver_used"] in ("gurobi", "highs")
        assert "solve_time_s" in data["last_optimization"]
        assert "charger_faults" in data
        assert isinstance(data["charger_faults"], list)
        assert "timestamp" in data
        assert data["depot_id"] == depot_id

    @patch("src.api.main.db_pool")
    @patch("src.api.main.verify_token")
    def test_alerts_charger_faults_when_status_faulted(
        self, mock_verify, mock_pool, client, mock_db_pool, depot_id
    ):
        """GIVEN StatusNotification Faulted with PowerMeterFailure WHEN GET alerts THEN charger_faults contains entry."""
        mock_verify.return_value = {"sub": "test"}
        pool, conn = mock_db_pool
        run_id = uuid4()
        charger_id = uuid4()
        now = datetime.utcnow()

        conn.fetchval = AsyncMock(return_value=1)
        conn.fetchrow = AsyncMock(
            return_value={
                "run_id": run_id,
                "run_time": now,
                "status": "optimal",
                "solver_used": "gurobi",
                "solve_time_s": 10.0,
            }
        )
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "charger_id": charger_id,
                    "ocpp_id": "CP001",
                    "connector_id": 1,
                    "fault_code": "PowerMeterFailure",
                    "timestamp": now,
                }
            ]
        )

        with patch("src.api.main.db_pool", pool):
            response = client.get(
                f"/depots/{depot_id}/alerts",
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["charger_faults"]) >= 1
        fault = next(
            (f for f in data["charger_faults"] if f.get("fault_code") == "PowerMeterFailure"),
            None,
        )
        assert fault is not None
        assert fault["ocpp_id"] == "CP001"
        assert fault["connector_id"] == 1
