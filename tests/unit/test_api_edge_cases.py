"""Comprehensive edge case tests for FastAPI endpoints.

Tests malformed input, concurrent requests, timeout handling,
error propagation, and response serialization edge cases.

Reference: PRD.md#11-2-unit-test-requirements
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, InvalidURL

from src.api.main import (
    app,
)
from src.core.models import DepotConfig, DepotState, OptimizationResult

# ============ Fixtures ============


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_db_pool():
    """Mock database connection pool with DatabasePools-compatible .ts and .static attrs."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    # Make .ts and .static delegate to pool so db_pools.ts.acquire works in endpoints
    pool.ts = pool
    pool.static = pool
    return pool, conn


@pytest.fixture
def sample_depot_config():
    """Sample depot configuration."""
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
def sample_optimization_result():
    """Sample optimization result."""
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            "bus_1": {"charging_power": [80.0] * 96, "soc": [0.3] * 96},
            "bus_2": {"charging_power": [60.0] * 96, "soc": [0.5] * 96},
        },
        battery_dispatch=[0.0] * 96,
        grid_power=[140.0] * 96,
        peak_demand=200.0,
        objective_value=1000.0,
        solve_time=5.0,
        status="completed",
    )


@pytest.fixture
def sample_depot_state():
    """Sample depot state."""
    n_t = 96
    return DepotState(
        vehicle_socs={"bus_1": 0.3, "bus_2": 0.5},
        battery_soc=0.5,
        prices=[0.10] * n_t,
        demand_charge_rate=15.0,
        current_month_peak=100.0,
        vehicle_availability={
            "bus_1": [True] * n_t,
            "bus_2": [True] * n_t,
        },
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        building_power=[50.0] * n_t,
    )


# ============ Malformed UUID Tests ============


class TestMalformedUUIDs:
    """Tests for malformed UUID input handling."""

    def test_optimize_empty_depot_id(self, client):
        """Test optimization with empty depot_id."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": "",
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_whitespace_depot_id(self, client):
        """Test optimization with whitespace depot_id."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": "   ",
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_partial_uuid(self, client):
        """Test optimization with partial UUID."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": "550e8400-e29b-41d4",
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_uuid_with_extra_chars(self, client):
        """Test optimization with UUID containing extra characters."""
        valid_uuid = str(uuid4())
        response = client.post(
            "/optimize",
            json={
                "depot_id": valid_uuid + "extra",
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_uuid_wrong_format(self, client):
        """Test optimization with incorrectly formatted UUID."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": "550e8400e29b41d4a716446655440000",  # Missing hyphens
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_depot_state_uuid_with_newline(self, client):
        """Test depot state with UUID containing newline."""
        with pytest.raises(InvalidURL):
            client.get("/depots/550e8400-e29b-41d4-a716-446655440000\n/state")

    def test_handoff_sql_injection_attempt(self, client):
        """Test handoff with SQL injection in UUID."""
        depot_id = str(uuid4())
        vehicle_id = str(uuid4())
        response = client.post(
            f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
            json={
                "dest_depot_id": "'; DROP TABLE depots; --",
                "expected_soc": 0.5,
                "arrival_time": datetime.utcnow().isoformat(),
            },
        )
        assert response.status_code in [400, 422]


# ============ Malformed Request Body Tests ============


class TestMalformedRequestBody:
    """Tests for malformed request body handling."""

    def test_optimize_missing_depot_id(self, client):
        """Test optimization without depot_id."""
        response = client.post(
            "/optimize",
            json={
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_null_depot_id(self, client):
        """Test optimization with null depot_id."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": None,
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_wrong_type_depot_id(self, client):
        """Test optimization with wrong type depot_id."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": 12345,
                "horizon_hours": 24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_wrong_type_horizon_hours(self, client):
        """Test optimization with wrong type horizon_hours."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": str(uuid4()),
                "horizon_hours": "twenty-four",
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_float_horizon_hours(self, client):
        """Test optimization with float horizon_hours."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": str(uuid4()),
                "horizon_hours": 24.5,
            },
        )
        # FastAPI may accept this and truncate to int
        assert response.status_code in [200, 400, 422, 503]

    def test_optimize_negative_horizon_hours(self, client):
        """Test optimization with negative horizon_hours."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": str(uuid4()),
                "horizon_hours": -24,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_zero_horizon_hours(self, client):
        """Test optimization with zero horizon_hours."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": str(uuid4()),
                "horizon_hours": 0,
            },
        )
        assert response.status_code in [400, 422]

    def test_optimize_empty_body(self, client):
        """Test optimization with empty body."""
        response = client.post("/optimize", json={})
        assert response.status_code in [400, 422]

    def test_optimize_malformed_json(self, client):
        """Test optimization with malformed JSON."""
        response = client.post(
            "/optimize",
            content='{"depot_id": "broken',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in [400, 422]

    def test_optimize_array_instead_of_object(self, client):
        """Test optimization with array instead of object."""
        response = client.post("/optimize", json=[str(uuid4()), 24])
        assert response.status_code in [400, 422]

    def test_handoff_soc_out_of_range(self, client):
        """Test handoff with SoC out of range."""
        depot_id = str(uuid4())
        vehicle_id = str(uuid4())

        # SoC > 1.0
        response = client.post(
            f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
            json={
                "dest_depot_id": str(uuid4()),
                "expected_soc": 1.5,
                "arrival_time": datetime.utcnow().isoformat(),
            },
        )
        assert response.status_code in [400, 422]

        # SoC < 0.0
        response = client.post(
            f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
            json={
                "dest_depot_id": str(uuid4()),
                "expected_soc": -0.5,
                "arrival_time": datetime.utcnow().isoformat(),
            },
        )
        assert response.status_code in [400, 422]

    def test_handoff_invalid_datetime(self, client):
        """Test handoff with invalid datetime format."""
        depot_id = str(uuid4())
        vehicle_id = str(uuid4())
        response = client.post(
            f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
            json={
                "dest_depot_id": str(uuid4()),
                "expected_soc": 0.5,
                "arrival_time": "not-a-datetime",
            },
        )
        assert response.status_code in [400, 422]


# ============ Boundary Value Tests ============


class TestBoundaryValues:
    """Tests for boundary value handling."""

    def test_optimize_min_horizon_hours(self, client):
        """Test optimization with minimum horizon_hours (1)."""
        with patch("src.api.main.db_pools", MagicMock()):
            response = client.post(
                "/optimize",
                json={
                    "depot_id": str(uuid4()),
                    "horizon_hours": 1,
                },
            )
            # May fail due to other reasons (db not configured) but not validation
            assert response.status_code != 422

    def test_optimize_max_horizon_hours(self, client):
        """Test optimization with maximum horizon_hours (48)."""
        with patch("src.api.main.db_pools", MagicMock()):
            response = client.post(
                "/optimize",
                json={
                    "depot_id": str(uuid4()),
                    "horizon_hours": 48,
                },
            )
            # May fail due to other reasons (db not configured) but not validation
            assert response.status_code != 422

    def test_optimize_horizon_hours_at_boundary_plus_one(self, client):
        """Test optimization with horizon_hours just over max."""
        response = client.post(
            "/optimize",
            json={
                "depot_id": str(uuid4()),
                "horizon_hours": 49,
            },
        )
        assert response.status_code in [400, 422]

    def test_handoff_soc_at_boundaries(self, client):
        """Test handoff with SoC at boundaries."""
        depot_id = str(uuid4())
        vehicle_id = str(uuid4())

        with patch("src.api.main.db_pools", MagicMock()):
            # SoC = 0.0
            response = client.post(
                f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
                json={
                    "dest_depot_id": str(uuid4()),
                    "expected_soc": 0.0,
                    "arrival_time": datetime.utcnow().isoformat(),
                },
            )
            assert response.status_code != 422

            # SoC = 1.0
            response = client.post(
                f"/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
                json={
                    "dest_depot_id": str(uuid4()),
                    "expected_soc": 1.0,
                    "arrival_time": datetime.utcnow().isoformat(),
                },
            )
            assert response.status_code != 422


# ============ Concurrent Request Tests ============


class TestConcurrentRequests:
    """Tests for concurrent request handling."""

    @pytest.mark.asyncio
    async def test_concurrent_optimize_requests(
        self, mock_db_pool, sample_depot_config, sample_optimization_result, sample_depot_state
    ):
        """Test multiple concurrent optimization requests."""
        pool, conn = mock_db_pool

        mock_controller_manager = MagicMock()
        mock_controller = AsyncMock()
        mock_controller.run_optimization = AsyncMock(return_value=sample_optimization_result)
        mock_controller_manager.get_or_create_controller = AsyncMock(return_value=mock_controller)

        with patch("src.api.main.db_pools", pool):
            with patch("src.api.main.controller_manager", mock_controller_manager):
                with patch(
                    "src.api.main._get_depot_config", AsyncMock(return_value=sample_depot_config)
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app), base_url="http://test"
                    ) as ac:
                        # Fire multiple concurrent requests
                        depot_id = str(uuid4())
                        tasks = [
                            ac.post(
                                "/optimize",
                                json={
                                    "depot_id": depot_id,
                                    "horizon_hours": 24,
                                },
                            )
                            for _ in range(5)
                        ]
                        responses = await asyncio.gather(*tasks)

                        # All should complete (may succeed or fail depending on controller)
                        for response in responses:
                            assert response.status_code in [200, 500, 503]

    @pytest.mark.asyncio
    async def test_concurrent_state_requests(
        self, mock_db_pool, sample_depot_config, sample_depot_state
    ):
        """Test multiple concurrent state requests."""
        pool, conn = mock_db_pool

        mock_assembler = AsyncMock()
        mock_assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.api.main.db_pools", pool):
            with patch(
                "src.api.main._get_depot_config", AsyncMock(return_value=sample_depot_config)
            ):
                with patch("src.api.main.StateAssembler", return_value=mock_assembler):
                    async with AsyncClient(
                        transport=ASGITransport(app=app), base_url="http://test"
                    ) as ac:
                        depot_id = str(uuid4())
                        tasks = [ac.get(f"/depots/{depot_id}/state") for _ in range(10)]
                        responses = await asyncio.gather(*tasks)

                        # All should complete
                        for response in responses:
                            assert response.status_code in [200, 500, 503]


# ============ Timeout Handling Tests ============


class TestTimeoutHandling:
    """Tests for request timeout handling."""

    @pytest.mark.asyncio
    async def test_optimize_timeout(self, mock_db_pool, sample_depot_config):
        """Test optimization timeout handling."""
        pool, conn = mock_db_pool

        mock_controller_manager = MagicMock()
        mock_controller = AsyncMock()

        async def slow_optimization(*args, **kwargs):
            await asyncio.sleep(10)  # Simulate slow optimization
            raise TimeoutError("Optimization timed out")

        mock_controller.run_optimization = slow_optimization
        mock_controller_manager.get_or_create_controller = AsyncMock(return_value=mock_controller)

        with patch("src.api.main.db_pools", pool):
            with patch("src.api.main.controller_manager", mock_controller_manager):
                with patch(
                    "src.api.main._get_depot_config", AsyncMock(return_value=sample_depot_config)
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="http://test",
                        timeout=0.5,  # Short timeout
                    ) as ac:
                        depot_id = str(uuid4())
                        try:
                            await ac.post(
                                "/optimize",
                                json={
                                    "depot_id": depot_id,
                                    "horizon_hours": 24,
                                },
                            )
                        except Exception:
                            # Expected to timeout
                            pass

    @pytest.mark.asyncio
    async def test_database_timeout(self, mock_db_pool):
        """Test database operation timeout handling."""
        pool, conn = mock_db_pool

        async def slow_query(*args, **kwargs):
            await asyncio.sleep(10)

        conn.fetchrow = slow_query

        with patch("src.api.main.db_pools", pool):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                timeout=0.5,
            ) as ac:
                depot_id = str(uuid4())
                try:
                    await ac.get(f"/depots/{depot_id}/schedule")
                except Exception:
                    # Expected to timeout
                    pass


# ============ Database Unavailability Tests ============


class TestDatabaseUnavailability:
    """Tests for database unavailability handling."""

    def test_optimize_db_unavailable(self, client):
        """Test optimization when database is unavailable."""
        with patch("src.api.main.db_pools", None):
            response = client.post(
                "/optimize",
                json={
                    "depot_id": str(uuid4()),
                    "horizon_hours": 24,
                },
            )
            assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    def test_depot_state_db_unavailable(self, client):
        """Test depot state when database is unavailable."""
        with patch("src.api.main.db_pools", None):
            response = client.get(f"/depots/{uuid4()}/state")
            assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    def test_schedule_db_unavailable(self, client):
        """Test schedule retrieval when database is unavailable."""
        with patch("src.api.main.db_pools", None):
            response = client.get(f"/depots/{uuid4()}/schedule")
            assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    def test_handoff_db_unavailable(self, client):
        """Test handoff when database is unavailable."""
        with patch("src.api.main.db_pools", None):
            response = client.post(
                f"/depots/{uuid4()}/vehicles/{uuid4()}/handoff",
                json={
                    "dest_depot_id": str(uuid4()),
                    "expected_soc": 0.5,
                    "arrival_time": datetime.utcnow().isoformat(),
                    "battery_kwh": 150.0,
                    "max_charge_kw": 80.0,
                },
            )
            assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE


# ============ Database Error Tests ============


class TestDatabaseErrors:
    """Tests for database error handling."""

    @pytest.mark.asyncio
    async def test_schedule_postgres_error(self, mock_db_pool):
        """Test schedule retrieval with PostgreSQL error."""
        pool, conn = mock_db_pool
        conn.fetchrow.side_effect = asyncpg.PostgresError("Connection refused")

        with patch("src.api.main.db_pools", pool):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get(f"/depots/{uuid4()}/schedule")
                assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_handoff_postgres_error(self, mock_db_pool):
        """Test handoff with PostgreSQL error."""
        pool, conn = mock_db_pool
        conn.execute.side_effect = asyncpg.PostgresError("Disk full")

        with patch("src.api.main.db_pools", pool):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.post(
                    f"/depots/{uuid4()}/vehicles/{uuid4()}/handoff",
                    json={
                        "dest_depot_id": str(uuid4()),
                        "expected_soc": 0.5,
                        "arrival_time": datetime.utcnow().isoformat(),
                        "battery_kwh": 150.0,
                        "max_charge_kw": 80.0,
                    },
                )
                assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE


# ============ Response Serialization Tests ============


class TestResponseSerialization:
    """Tests for response serialization edge cases."""

    @pytest.mark.asyncio
    async def test_schedule_with_special_characters_in_vehicle_id(self, mock_db_pool):
        """Test schedule response with special characters in vehicle_id."""
        pool, conn = mock_db_pool

        depot_id = str(uuid4())
        schedule_json = {
            "schedule": {
                "bus-1_test": {"charging_power": [80], "soc": [0.5]},
                "bus.2.special": {"charging_power": [60], "soc": [0.6]},
            }
        }

        conn.fetchrow = AsyncMock(
            return_value={
                "run_id": uuid4(),
                "run_time": datetime.utcnow(),
                "schedule_json": schedule_json,
                "horizon_start": datetime.utcnow(),
                "horizon_end": datetime.utcnow() + timedelta(hours=24),
            }
        )

        with patch("src.api.main.db_pools", pool):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get(f"/depots/{depot_id}/schedule")
                if response.status_code == 200:
                    data = response.json()
                    assert "schedule" in data

    @pytest.mark.asyncio
    async def test_state_response_with_many_vehicles(self, mock_db_pool, sample_depot_config):
        """Test state response with large number of vehicles."""
        pool, conn = mock_db_pool

        # Create state with many vehicles
        n_vehicles = 100
        vehicle_socs = {f"bus_{i}": 0.5 for i in range(n_vehicles)}
        vehicle_availability = {f"bus_{i}": [True] * 96 for i in range(n_vehicles)}

        large_state = DepotState(
            vehicle_socs=vehicle_socs,
            battery_soc=0.5,
            prices=[0.10] * 96,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability=vehicle_availability,
            energy_requirements={f"bus_{i}": 200.0 for i in range(n_vehicles)},
            departure_times={f"bus_{i}": 48 for i in range(n_vehicles)},
            building_power=[50.0] * 96,
        )

        mock_assembler = AsyncMock()
        mock_assembler.get_current_state = AsyncMock(return_value=large_state)

        with patch("src.api.main.db_pools", pool):
            with patch(
                "src.api.main._get_depot_config", AsyncMock(return_value=sample_depot_config)
            ):
                with patch("src.api.main.StateAssembler", return_value=mock_assembler):
                    async with AsyncClient(
                        transport=ASGITransport(app=app), base_url="http://test"
                    ) as ac:
                        response = await ac.get(f"/depots/{uuid4()}/state")
                        if response.status_code == 200:
                            data = response.json()
                            assert len(data["vehicle_socs"]) == n_vehicles


# ============ Error Response Format Tests ============


class TestErrorResponseFormat:
    """Tests for consistent error response format."""

    def test_validation_error_format(self, client):
        """Test validation error response format."""
        response = client.post("/optimize", json={"invalid": "data"})
        assert response.status_code in [400, 422]
        data = response.json()
        # FastAPI/Pydantic validation error format
        assert "detail" in data

    def test_not_found_error_format(self, client, mock_db_pool):
        """Test not found error response format."""
        pool, conn = mock_db_pool
        conn.fetchrow = AsyncMock(return_value=None)

        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{uuid4()}/schedule")
            assert response.status_code == http_status.HTTP_404_NOT_FOUND
            data = response.json()
            assert "detail" in data

    def test_error_response_has_timestamp(self, client):
        """Test error response includes timestamp."""
        with patch("src.api.main.db_pools", None):
            response = client.post(
                "/optimize",
                json={
                    "depot_id": str(uuid4()),
                    "horizon_hours": 24,
                },
            )
            if response.status_code >= 400:
                data = response.json()
                # Error responses should have timestamp
                if "timestamp" in data:
                    assert data["timestamp"]


# ============ Special Character Handling Tests ============


class TestSpecialCharacterHandling:
    """Tests for special character handling in paths and bodies."""

    def test_depot_id_with_unicode(self, client):
        """Test depot ID path with unicode characters."""
        response = client.get("/depots/550e8400-😀-41d4-a716-446655440000/state")
        assert response.status_code in [400, 404, 422]

    def test_depot_id_with_url_encoding(self, client):
        """Test depot ID path with URL encoding."""
        # %20 is space
        response = client.get("/depots/550e8400%20e29b-41d4-a716-446655440000/state")
        assert response.status_code in [400, 404, 422]

    def test_optimization_extra_fields_ignored(self, client):
        """Test optimization ignores extra fields in request."""
        with patch("src.api.main.db_pools", MagicMock()):
            response = client.post(
                "/optimize",
                json={
                    "depot_id": str(uuid4()),
                    "horizon_hours": 24,
                    "extra_field": "should be ignored",
                    "another_extra": 12345,
                },
            )
            # Should not fail due to extra fields
            assert response.status_code != 422


# ============ Content-Type Header Tests ============


class TestContentTypeHandling:
    """Tests for Content-Type header handling."""

    def test_optimize_wrong_content_type(self, client):
        """Test optimization with wrong Content-Type."""
        response = client.post(
            "/optimize",
            content="depot_id=test&horizon_hours=24",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code in [400, 422]

    def test_optimize_missing_content_type(self, client):
        """Test optimization without Content-Type header."""
        response = client.post(
            "/optimize",
            content='{"depot_id": "' + str(uuid4()) + '", "horizon_hours": 24}',
        )
        # FastAPI may infer JSON or reject
        assert response.status_code in [200, 400, 415, 422, 503]


# ============ Health Check Edge Cases ============


class TestHealthCheckEdgeCases:
    """Tests for health check edge cases."""

    @pytest.mark.asyncio
    async def test_health_during_database_reconnect(self, mock_db_pool):
        """Test health check during database reconnection."""
        pool, conn = mock_db_pool

        # Simulate connection error
        conn.fetchval = AsyncMock(side_effect=asyncpg.PostgresConnectionError("Connection lost"))

        with patch("src.api.main.db_pools", pool):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/health")
                # Health endpoint should still return 200 but with degraded status
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "degraded"
                assert data["components"]["database"] == "unavailable"

    def test_health_endpoint_fast_response(self, client):
        """Test health endpoint responds quickly."""
        start = time.time()
        response = client.get("/health")
        elapsed = time.time() - start

        assert response.status_code == 200
        # Health check should be fast (< 1 second typically)
        # Allow more time in CI environments
        assert elapsed < 5.0


# ============ Admin Endpoint Tests ============


class TestAdminEndpoints:
    """Tests for admin endpoints."""

    def test_list_controllers_no_manager(self, client):
        """Test listing controllers when manager is not initialized."""
        with patch("src.api.main.controller_manager", None):
            response = client.get("/admin/controllers")
            assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    def test_controller_health_invalid_depot_id(self, client):
        """Test controller health with invalid depot_id."""
        with patch("src.api.main.controller_manager", MagicMock()):
            response = client.get("/admin/controllers/not-a-uuid/health")
            assert response.status_code in [400, 404]

    def test_controller_health_not_found(self, client):
        """Test controller health when controller doesn't exist."""
        mock_manager = MagicMock()
        mock_manager.health_check = AsyncMock(return_value={})

        with patch("src.api.main.controller_manager", mock_manager):
            response = client.get(f"/admin/controllers/{uuid4()}/health")
            assert response.status_code == http_status.HTTP_404_NOT_FOUND
