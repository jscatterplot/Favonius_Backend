"""Unit tests for FastAPI main application.

Reference: PRD.md#11-2-unit-test-requirements
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import status as http_status

from src.api.main import (
    HandoffRequest,
    OptimizationRequest,
    _require_depot_access,
    app,
    describe_database_target,
    resolve_database_url,
    validate_depot_id,
    validate_horizon_hours,
    validate_uuid,
)
from src.security.tenant_mirror import ensure_tenant_mirrored


@pytest.fixture(autouse=True)
def default_auth():
    """Inject admin auth for all tests in this module.

    Uses app.dependency_overrides (not @patch) because FastAPI captures the
    Depends() function object at module load time. The admin role bypasses
    depot_ids checks so tests with arbitrary depot UUIDs all pass auth.

    Restores any prior ``ensure_tenant_mirrored`` override (e.g. from
    ``tests/conftest.py``) on teardown so other test modules are not left
    without auth.
    """
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


class TestValidationUtilities:
    """Test input validation utilities."""

    def test_validate_uuid_valid(self):
        """Test UUID validation with valid UUID."""
        valid_uuid = str(uuid4())
        result = validate_uuid(valid_uuid, "test_id")
        assert result == valid_uuid

    def test_validate_uuid_invalid(self):
        """Test UUID validation with invalid UUID."""
        with pytest.raises(Exception):  # HTTPException
            validate_uuid("not-a-uuid", "test_id")

    def test_validate_depot_id_valid(self):
        """Test depot_id validation with valid UUID."""
        valid_uuid = str(uuid4())
        result = validate_depot_id(valid_uuid)
        assert result == valid_uuid

    def test_validate_horizon_hours_valid(self):
        """Test horizon_hours validation with valid range."""
        assert validate_horizon_hours(24) == 24
        assert validate_horizon_hours(1) == 1
        assert validate_horizon_hours(48) == 48

    def test_validate_horizon_hours_invalid(self):
        """Test horizon_hours validation with invalid range."""
        with pytest.raises(Exception):  # HTTPException
            validate_horizon_hours(0)
        with pytest.raises(Exception):
            validate_horizon_hours(49)


class TestOptimizeEndpoint:
    """Test /optimize endpoint."""

    @patch("src.api.main.controller_manager")
    @patch("src.api.main.db_pools")
    @patch("src.api.main._get_depot_config")
    def test_optimize_success(
        self,
        mock_get_config,
        mock_pool,
        mock_controller_manager,
        client,
        sample_depot_config,
        sample_optimization_result,
    ):
        """Test successful optimization."""
        mock_pool = MagicMock()
        mock_get_config.return_value = sample_depot_config

        mock_controller = AsyncMock()
        mock_controller.run_optimization = AsyncMock(return_value=sample_optimization_result)
        mock_controller_manager.get_or_create_controller = AsyncMock(return_value=mock_controller)

        with patch("src.api.main.db_pools", mock_pool):
            request = OptimizationRequest(
                depot_id=str(uuid4()),
                horizon_hours=24,
                force=False,
            )
            response = client.post("/optimize", json=request.model_dump())

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert "run_id" in data
        assert "objective_value" in data
        assert "schedule" in data

    def test_optimize_invalid_uuid(self, client):
        """Test optimization with invalid depot_id."""
        request = {"depot_id": "not-a-uuid", "horizon_hours": 24}
        response = client.post("/optimize", json=request)
        assert response.status_code in [400, http_status.HTTP_422_UNPROCESSABLE_ENTITY]

    def test_optimize_invalid_horizon(self, client):
        """Test optimization with invalid horizon_hours."""
        request = {
            "depot_id": str(uuid4()),
            "horizon_hours": 100,  # Out of range
        }
        response = client.post("/optimize", json=request)
        assert response.status_code in [400, http_status.HTTP_422_UNPROCESSABLE_ENTITY]


class TestDepotStateEndpoint:
    """Test /depots/{depot_id}/state endpoint."""

    @patch("src.api.main.db_pools")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.StateAssembler")
    def test_get_depot_state_success(
        self,
        mock_assembler_class,
        mock_get_config,
        mock_pool,
        client,
        sample_depot_config,
        sample_depot_state,
    ):
        """Test successful depot state retrieval."""
        mock_pool = MagicMock()
        mock_get_config.return_value = sample_depot_config

        mock_assembler = AsyncMock()
        mock_assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        mock_assembler_class.return_value = mock_assembler

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool):
            response = client.get(f"/depots/{depot_id}/state")

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["depot_id"] == depot_id
        assert "vehicle_socs" in data
        assert "battery_soc" in data
        assert "current_month_peak_kw" in data
        assert "current_price_kwh" in data

    def test_get_depot_state_invalid_uuid(self, client):
        """Test depot state with invalid depot_id."""
        response = client.get("/depots/not-a-uuid/state")
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


class TestDepotScheduleEndpoint:
    """Test /depots/{depot_id}/schedule endpoint."""

    @patch("src.api.main.db_pools")
    def test_get_schedule_success(self, mock_pool, client, mock_db_pool):
        """Test successful schedule retrieval."""
        pool, conn = mock_db_pool
        mock_pool = pool

        depot_id = str(uuid4())
        run_id = uuid4()
        schedule_json = {"schedule": {"bus_1": {"charging_power": [80, 80], "soc": [0.3, 0.4]}}}

        conn.fetchrow = AsyncMock(
            return_value={
                "run_id": run_id,
                "run_time": datetime.utcnow(),
                "schedule_json": schedule_json,
                "horizon_start": datetime.utcnow(),
                "horizon_end": datetime.utcnow() + timedelta(hours=24),
            }
        )

        with patch("src.api.main.db_pools", mock_pool):
            response = client.get(f"/depots/{depot_id}/schedule")

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["depot_id"] == depot_id
        assert data["run_id"] == str(run_id)
        assert "schedule" in data

    @patch("src.api.main.db_pools")
    def test_get_schedule_not_found(self, mock_pool, client, mock_db_pool):
        """Test schedule retrieval when no schedule exists."""
        pool, conn = mock_db_pool
        mock_pool = pool

        depot_id = str(uuid4())
        conn.fetchrow = AsyncMock(return_value=None)

        with patch("src.api.main.db_pools", mock_pool):
            response = client.get(f"/depots/{depot_id}/schedule")

        assert response.status_code == http_status.HTTP_404_NOT_FOUND


class TestDepotAlertsEndpoint:
    """Test GET /depots/{depot_id}/alerts (PRD §7.1, AT-16)."""

    @patch("src.api.main.db_pools")
    def test_get_alerts_success_with_last_optimization(
        self, mock_pool, client, mock_db_pool
    ):
        """Alerts returns last_optimization and charger_faults."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        run_id = uuid4()
        now = datetime.utcnow()

        conn.fetchval = AsyncMock(return_value=1)
        conn.fetchrow = AsyncMock(
            return_value={
                "run_id": run_id,
                "run_time": now,
                "status": "optimal",
                "solver_used": "gurobi",
                "solve_time_s": 12.3,
            }
        )
        conn.fetch = AsyncMock(return_value=[])

        with patch("src.api.main.db_pools", pool):
            response = client.get(
                f"/depots/{depot_id}/alerts",
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["depot_id"] == depot_id
        assert "timestamp" in data
        assert data["charger_faults"] == []
        assert data["last_optimization"] is not None
        assert data["last_optimization"]["run_id"] == str(run_id)
        assert data["last_optimization"]["status"] == "optimal"
        assert data["last_optimization"]["solver_used"] == "gurobi"
        assert data["last_optimization"]["solve_time_s"] == 12.3

    @patch("src.api.main.db_pools")
    def test_get_alerts_includes_charger_faults(self, mock_pool, client, mock_db_pool):
        """When connector_status has Faulted, charger_faults list is populated."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        charger_id = uuid4()
        now = datetime.utcnow()

        conn.fetchval = AsyncMock(return_value=1)
        conn.fetchrow = AsyncMock(
            return_value={
                "run_id": uuid4(),
                "run_time": now,
                "status": "optimal",
                "solver_used": "gurobi",
                "solve_time_s": 5.0,
            }
        )
        # fetch is called twice: (1) charger rows from static pool, (2) fault rows from ts pool
        conn.fetch = AsyncMock(
            side_effect=[
                # First call: charger rows (static pool — ocpp_id + charger_id mapping)
                [{"ocpp_id": "CP001", "charger_id": charger_id}],
                # Second call: fault rows (ts pool — connector_status table)
                [
                    {
                        "station_id": "CP001",
                        "connector_id": 1,
                        "status": "Faulted",
                        "error_code": "PowerMeterFailure",
                        "timestamp": now,
                    }
                ],
            ]
        )

        with patch("src.api.main.db_pools", pool):
            response = client.get(
                f"/depots/{depot_id}/alerts",
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert len(data["charger_faults"]) == 1
        assert data["charger_faults"][0]["ocpp_id"] == "CP001"
        assert data["charger_faults"][0]["fault_code"] == "PowerMeterFailure"
        assert data["charger_faults"][0]["connector_id"] == 1

    @patch("src.api.main.db_pools")
    def test_get_alerts_depot_not_found(self, mock_pool, client, mock_db_pool):
        """Alerts returns 404 when depot does not exist."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        conn.fetchval = AsyncMock(return_value=None)

        with patch("src.api.main.db_pools", pool):
            response = client.get(
                f"/depots/{depot_id}/alerts",
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == http_status.HTTP_404_NOT_FOUND

    def test_get_alerts_invalid_depot_id(self, client):
        """Alerts returns 400 for invalid depot_id."""
        response = client.get(
            "/depots/not-a-uuid/alerts",
            headers={"Authorization": "Bearer test"},
        )
        assert response.status_code in (
            http_status.HTTP_400_BAD_REQUEST,
            http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class TestHandoffEndpoint:
    """Test /depots/{depot_id}/vehicles/{vehicle_id}/handoff endpoint."""

    @patch("src.api.main.db_pools")
    def test_send_handoff_success(self, mock_pool, client, mock_db_pool):
        """Test successful handoff message."""
        pool, conn = mock_db_pool
        mock_pool = pool

        depot_id = str(uuid4())
        vehicle_id = str(uuid4())
        dest_depot_id = str(uuid4())

        conn.execute = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "external_id": "bus_1",
                "battery_kwh": 150.0,
                "max_charge_kw": 80.0,
            }
        )

        request = {
            "dest_depot_id": dest_depot_id,
            "expected_soc": 0.35,
            "arrival_time": (datetime.utcnow() + timedelta(hours=2)).isoformat(),
            "battery_kwh": 150.0,
            "max_charge_kw": 80.0,
        }

        with patch("src.api.main.db_pools", mock_pool):
            response = client.post(
                f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff", json=request
            )

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert "message_id" in data
        assert data["status"] == "sent"

    def test_send_handoff_invalid_uuid(self, client):
        """Test handoff with invalid UUIDs."""
        depot_id = "not-a-uuid"
        vehicle_id = str(uuid4())
        request = {
            "dest_depot_id": str(uuid4()),
            "expected_soc": 0.35,
            "arrival_time": datetime.utcnow().isoformat(),
            "battery_kwh": 150.0,
            "max_charge_kw": 80.0,
        }
        response = client.post(f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff", json=request)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


class TestHealthEndpoint:
    """Test /health endpoint."""

    @patch("src.api.main.check_database_health")
    @patch("src.api.main.check_ocpp_server_health")
    def test_health_check_success(self, mock_ocpp_health, mock_db_health, client):
        """Test successful health check."""
        mock_db_health.return_value = "healthy"
        mock_ocpp_health.return_value = "unknown"

        response = client.get("/health")

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["status"] in ["healthy", "degraded"]
        assert "components" in data
        assert "database" in data["components"]
        assert "ocpp_server" in data["components"]
        assert "controller_manager" in data["components"]

    @patch("src.api.main.check_database_health")
    @patch("src.api.main.check_ocpp_server_health")
    def test_health_check_database_unavailable(self, mock_ocpp_health, mock_db_health, client):
        """Test health check with database unavailable returns degraded status."""
        mock_db_health.return_value = "unavailable"
        mock_ocpp_health.return_value = "unknown"

        response = client.get("/health")

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "degraded"
        assert data["components"]["database"] == "unavailable"

    @patch("src.api.main.controller_manager", None)
    @patch("src.api.main.check_database_health")
    @patch("src.api.main.check_ocpp_server_health")
    def test_health_check_controller_manager_unavailable(
        self, mock_ocpp_health, mock_db_health, client
    ):
        """Test health check returns degraded when controller manager is None."""
        mock_db_health.return_value = "healthy"
        mock_ocpp_health.return_value = "unknown"

        response = client.get("/health")

        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "degraded"
        assert data["components"]["controller_manager"] == "unavailable"
        assert data["components"]["database"] == "healthy"


class TestErrorHandling:
    """Test error handling and exception handlers."""

    def test_validation_error_handler(self, client):
        """Test Pydantic validation error handling."""
        # Invalid request body
        response = client.post("/optimize", json={"invalid": "data"})
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        data = response.json()
        assert "detail" in data
        assert "error_code" in data

    @patch("src.api.main.db_pools", None)
    def test_database_unavailable(self, client):
        """Test handling when database is unavailable."""
        request = OptimizationRequest(
            depot_id=str(uuid4()),
            horizon_hours=24,
        )
        response = client.post("/optimize", json=request.model_dump())
        assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE


class TestRequestModels:
    """Test Pydantic request/response models."""

    def test_optimization_request_validation(self):
        """Test OptimizationRequest model validation."""
        # Valid request
        request = OptimizationRequest(
            depot_id=str(uuid4()),
            horizon_hours=24,
            force=False,
        )
        assert request.depot_id
        assert request.horizon_hours == 24

        # Invalid horizon_hours
        with pytest.raises(Exception):  # ValidationError
            OptimizationRequest(
                depot_id=str(uuid4()),
                horizon_hours=100,
            )

    def test_handoff_request_validation(self):
        """Test HandoffRequest model validation."""
        # Valid request
        request = HandoffRequest(
            dest_depot_id=str(uuid4()),
            expected_soc=0.35,
            arrival_time=datetime.utcnow() + timedelta(hours=2),
            battery_kwh=150.0,
            max_charge_kw=80.0,
        )
        assert request.expected_soc == 0.35

        # Invalid SoC
        with pytest.raises(Exception):  # ValidationError
            HandoffRequest(
                dest_depot_id=str(uuid4()),
                expected_soc=1.5,  # > 1.0
                arrival_time=datetime.utcnow(),
                battery_kwh=150.0,
                max_charge_kw=80.0,
            )


class TestDatabaseConfiguration:
    """Test database URL resolution helpers."""

    def test_resolve_database_url_prefers_database_url(self, monkeypatch):
        """DATABASE_URL should take precedence over TIMESCALE_SERVICE_URL."""
        monkeypatch.setenv("TIMESCALE_SERVICE_URL", "postgresql://ts_user:ts_pw@timescale:5432/tsdb")
        monkeypatch.setenv("DATABASE_URL", "postgresql://api_user:api_pw@api-db:5432/apidb")

        database_url, source = resolve_database_url()

        assert source == "DATABASE_URL"
        assert database_url == "postgresql://api_user:api_pw@api-db:5432/apidb"

    def test_resolve_database_url_uses_timescale_service_url_fallback(self, monkeypatch):
        """TIMESCALE_SERVICE_URL should be used when DATABASE_URL is absent."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("TIMESCALE_SERVICE_URL", "postgresql://ts_user:ts_pw@timescale:5432/tsdb")

        database_url, source = resolve_database_url()

        assert source == "TIMESCALE_SERVICE_URL"
        assert database_url == "postgresql://ts_user:ts_pw@timescale:5432/tsdb"

    def test_resolve_database_url_raises_when_unset(self, monkeypatch):
        """Helper should fail fast when no DB URL env var is configured."""
        monkeypatch.delenv("TIMESCALE_SERVICE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(RuntimeError):
            resolve_database_url()

    def test_describe_database_target_masks_password(self):
        """Log descriptor exposes only port; user/host-prefix/db are masked."""
        description = describe_database_target(
            "postgresql://postgres:super_secret@db.example.com:5432/favonius"
        )
        # Sensitive fields must not appear
        assert "super_secret" not in description
        assert "postgres" not in description
        assert "favonius" not in description
        # Port is the only value exposed
        assert "port=5432" in description
        # Domain suffix is partially shown; full hostname is not
        assert "example.com" in description
        assert "db.example.com" not in description
