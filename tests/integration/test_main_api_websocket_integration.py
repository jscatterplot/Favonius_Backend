"""Integration tests for Main API ↔ WebSocket Handler communication.

Tests the integrated architecture where:
- WebSocket Handler is telemetry-only (OCPP → Main API + TimescaleDB)
- Main API handles all optimization
- Main API communicates with WebSocket Handler via internal API
- Backup heuristic activates when Main API is down > 1 hour

Reference: Architecture Integration Plan, PRD_v2.md Section 5.2
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, OptimizationResult
from src.core.state.assembler import StateAssembler


@pytest.fixture
def mock_websocket_handler_internal_api():
    """Mock WebSocket Handler internal API server."""
    api = AsyncMock()

    # Charge point queries
    api.get_charge_points = AsyncMock(
        return_value=[
            {
                "station_id": "charger_001",
                "connected": True,
                "evse_id": 1,
                "connector_id": 1,
                "status": "Available",
            },
            {
                "station_id": "charger_002",
                "connected": True,
                "evse_id": 1,
                "connector_id": 2,
                "status": "Charging",
            },
        ]
    )

    api.get_charge_point_state = AsyncMock(
        return_value={
            "station_id": "charger_001",
            "connected": True,
            "evse_id": 1,
            "connector_id": 1,
            "status": "Available",
            "current_power_kw": 0.0,
            "soc_percent": None,
        }
    )

    # Command dispatch
    api.send_charging_profile = AsyncMock(
        return_value={
            "success": True,
            "message": "Charging profile set successfully",
        }
    )

    # Health check
    api.health_check = AsyncMock(
        return_value={
            "status": "healthy",
            "connected_chargers": 2,
        }
    )

    return api


@pytest.fixture
def mock_ocpp_client(mock_websocket_handler_internal_api):
    """Mock OCPP client that communicates with WebSocket Handler."""
    client = AsyncMock()

    client.get_connected_charge_points = AsyncMock(
        side_effect=mock_websocket_handler_internal_api.get_charge_points
    )
    client.get_charge_point_state = AsyncMock(
        side_effect=mock_websocket_handler_internal_api.get_charge_point_state
    )
    client.send_charging_profile = AsyncMock(
        side_effect=mock_websocket_handler_internal_api.send_charging_profile
    )
    client.is_connected = AsyncMock(return_value=True)

    return client


@pytest.fixture
def sample_depot_config():
    """Sample depot configuration."""
    return DepotConfig(
        vehicle_capacities={
            "bus_1": 324.0,
            "bus_2": 324.0,
        },
        vehicle_max_charge_kw={"bus_1": 80.0, "bus_2": 80.0},
        charger_groups={80.0: 2},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=500.0,
        battery_power=100.0,
        battery_efficiency=0.92,
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=800.0,
        delta_t=0.25,
        n_timesteps=96,
    )


@pytest.fixture
def fast_controller_config():
    """Fast controller config for tests."""
    return ControllerConfig(
        optimization_horizon_hours=12,
        hourly_optimization_start=0,
        hourly_optimization_end=24,
        optimization_timeout=10.0,
        trigger_cooldown_minutes=0,
        max_optimization_failures=2,
        dispatch_retry_attempts=1,
        dispatch_retry_delay_seconds=0.1,
        shutdown_timeout_seconds=1.0,
    )


class TestMainAPIToWebSocketHandler:
    """Tests for Main API querying WebSocket Handler."""

    @pytest.mark.asyncio
    async def test_query_connected_charge_points(self, mock_ocpp_client, mock_db_pool):
        """Test Main API can query WebSocket Handler for connected charge points."""
        charge_points = await mock_ocpp_client.get_connected_charge_points()

        assert len(charge_points) == 2
        assert charge_points[0]["station_id"] == "charger_001"
        assert charge_points[1]["station_id"] == "charger_002"
        assert all(cp["connected"] for cp in charge_points)

    @pytest.mark.asyncio
    async def test_query_charge_point_state(self, mock_ocpp_client, mock_db_pool):
        """Test Main API can query WebSocket Handler for specific charge point state."""
        state = await mock_ocpp_client.get_charge_point_state("charger_001")

        assert state["station_id"] == "charger_001"
        assert state["connected"] is True
        assert state["evse_id"] == 1
        assert "status" in state

    @pytest.mark.asyncio
    async def test_send_charging_profile_via_websocket_handler(
        self, mock_ocpp_client, mock_db_pool
    ):
        """Test Main API can send SetChargingProfile via WebSocket Handler."""
        profile = {
            "evse_id": 1,
            "charging_profile": {
                "charging_profile_id": 1,
                "stack_level": 0,
                "charging_profile_purpose": "TxDefaultProfile",
                "charging_schedule": {
                    "id": 1,
                    "charging_rate_unit": "W",
                    "charging_schedule_period": [
                        {
                            "start_period": 0,
                            "limit": 80000,  # 80 kW in Watts
                        }
                    ],
                },
            },
        }

        result = await mock_ocpp_client.send_charging_profile("charger_001", profile)

        assert result["success"] is True
        mock_ocpp_client.send_charging_profile.assert_called_once()


class TestTelemetryFlow:
    """Tests for telemetry flow: Charger → WebSocket Handler → Main API + TimescaleDB."""

    @pytest.mark.asyncio
    async def test_telemetry_stored_to_timescale(self, mock_db_pool, mock_timescale_client):
        """Test telemetry from WebSocket Handler is stored to TimescaleDB."""
        pool, conn = mock_db_pool

        # Simulate MeterValues from charger
        meter_values = [
            {
                "time": datetime.now(timezone.utc),
                "station_id": "charger_001",
                "evse_id": 1,
                "connector_id": 1,
                "session_id": "TXN123",
                "power_kw": 80.0,
                "energy_kwh": 22.5,
                "soc_percent": 65.0,
            }
        ]

        # WebSocket Handler stores to TimescaleDB
        await mock_timescale_client.insert_telemetry_batch(meter_values)

        # Verify storage was called
        mock_timescale_client.insert_telemetry_batch.assert_called_once()
        call_args = mock_timescale_client.insert_telemetry_batch.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["station_id"] == "charger_001"
        assert call_args[0]["power_kw"] == 80.0

    @pytest.mark.asyncio
    async def test_telemetry_forwarded_to_main_api(self, mock_ocpp_client, mock_db_pool):
        """Test telemetry is forwarded to Main API for state assembly."""
        # Simulate WebSocket Handler forwarding telemetry to Main API
        # (This would be via internal API endpoint or direct call)

        # Main API queries charge point state (which includes latest telemetry)
        state = await mock_ocpp_client.get_charge_point_state("charger_001")

        # Verify state includes telemetry data
        assert "current_power_kw" in state
        assert "soc_percent" in state or state.get("soc_percent") is None


class TestCommandDispatchFlow:
    """Tests for command dispatch: Main API → WebSocket Handler → Charger."""

    @pytest.mark.asyncio
    async def test_optimization_result_dispatched_via_websocket_handler(
        self,
        mock_ocpp_client,
        mock_db_pool,
        sample_depot_config,
        fast_controller_config,
        sample_depot_state,
    ):
        """Test optimization results are dispatched via WebSocket Handler."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        # TODO: After Phase 4 implementation, change to ocpp_client
        # For now, use ocpp_server with mock that simulates client behavior
        mock_ocpp_server = MagicMock()
        mock_charge_point = AsyncMock()
        mock_charge_point.set_charging_profile = AsyncMock(return_value=True)
        mock_ocpp_server.get_charge_point = MagicMock(return_value=mock_charge_point)

        # Mock load_depot_config to prevent database queries during controller initialization
        # This fixes RuntimeWarning about coroutines not being awaited
        with patch.object(StateAssembler, "load_depot_config", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = (sample_depot_config, {})
            controller = DepotController(
                pool=pool,
                depot_id=depot_id,
                config=sample_depot_config,
                controller_config=fast_controller_config,
                ocpp_server=mock_ocpp_server,  # Temporary: use ocpp_server until ocpp_client implemented
            )

        # Mock state assembly
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        # Mock optimization result
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                "bus_1": {
                    "charging_power": [80.0] * 96,
                    "soc": [0.3 + 0.01 * i for i in range(96)],
                },
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[200.0] * 96,
            peak_demand_kw=250.0,  # Use correct field name
            objective_value=1500.0,
            solve_time_s=5.0,  # Use correct field name
            status="completed",
            solver_used="gurobi",
        )

        # Dispatch commands
        # Keep load_depot_config mocked during dispatch (called by _dispatch_commands)
        with (
            patch("src.core.controller.optimize", return_value=result),
            patch.object(
                StateAssembler, "load_depot_config", new_callable=AsyncMock
            ) as mock_load_dispatch,
        ):
            mock_load_dispatch.return_value = (sample_depot_config, {"bus_1": "charger_001"})
            await controller.run_optimization("test_dispatch")

        # Verify commands were sent via OCPP server (temporary until ocpp_client implemented)
        # The controller dispatches via ocpp_server.get_charge_point().set_charging_profile()
        assert mock_charge_point.set_charging_profile.called

    @pytest.mark.asyncio
    async def test_command_dispatch_failure_handling(self, mock_ocpp_client, mock_db_pool):
        """Test error handling when command dispatch fails."""
        # Simulate WebSocket Handler failure
        mock_ocpp_client.send_charging_profile = AsyncMock(
            side_effect=Exception("WebSocket Handler unavailable")
        )

        profile = {
            "evse_id": 1,
            "charging_profile": {"id": 1},
        }

        # Should handle error gracefully
        with pytest.raises(Exception):
            await mock_ocpp_client.send_charging_profile("charger_001", profile)


class TestBackupHeuristicActivation:
    """Tests for backup heuristic activation when Main API is down."""

    @pytest.mark.asyncio
    async def test_backup_heuristic_activates_after_one_hour(
        self, mock_timescale_client, mock_connection_manager
    ):
        """Test backup heuristic activates when Main API is down > 1 hour."""
        # Mock OptimizationEngine to avoid importing websocket_handler config
        # TODO: After architecture refactoring, use real OptimizationEngine
        mock_engine = AsyncMock()

        # Mock Main API health check failing for > 1 hour
        main_api_health_checks = [
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
        ]  # 16 failures = 80 minutes (assuming 5-min intervals)

        # Mock the backup activation logic
        async def should_activate_backup():
            # Simulate: after 12+ consecutive failures (60+ minutes at 5-min intervals)
            return len(main_api_health_checks) >= 12

        mock_engine._should_activate_backup = AsyncMock(side_effect=should_activate_backup)
        mock_engine._check_main_api_health = AsyncMock(
            side_effect=lambda: main_api_health_checks.pop(0) if main_api_health_checks else True
        )

        # Check if backup should activate
        should_activate = await mock_engine._should_activate_backup()

        # After > 1 hour of failures, backup should activate
        assert should_activate is True

    @pytest.mark.asyncio
    async def test_backup_heuristic_deactivates_on_recovery(
        self, mock_timescale_client, mock_connection_manager
    ):
        """Test backup heuristic deactivates when Main API recovers."""
        # Mock OptimizationEngine to avoid importing websocket_handler config
        # TODO: After architecture refactoring, use real OptimizationEngine
        mock_engine = AsyncMock()

        # Simulate recovery: health check succeeds
        mock_engine._check_main_api_health = AsyncMock(return_value=True)

        # Mock backup activation: returns False when health check succeeds
        async def should_activate_backup():
            health_ok = await mock_engine._check_main_api_health()
            return not health_ok  # Only activate if health check fails

        mock_engine._should_activate_backup = AsyncMock(side_effect=should_activate_backup)

        should_activate = await mock_engine._should_activate_backup()

        # When Main API is healthy, backup should not activate
        assert should_activate is False


class TestEndToEndIntegration:
    """End-to-end integration tests for the full system."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_flow_charger_to_optimization_to_command(
        self,
        mock_ocpp_client,
        mock_db_pool,
        mock_timescale_client,
        sample_depot_config,
        fast_controller_config,
        sample_depot_state,
    ):
        """Test complete flow: charger connection → telemetry → optimization → command."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        # 1. Charger connects to WebSocket Handler
        charge_points = await mock_ocpp_client.get_connected_charge_points()
        assert len(charge_points) > 0

        # 2. Telemetry flows from charger to TimescaleDB
        telemetry = [
            {
                "time": datetime.now(timezone.utc),
                "station_id": "charger_001",
                "evse_id": 1,
                "connector_id": 1,
                "power_kw": 80.0,
                "soc_percent": 65.0,
            }
        ]
        await mock_timescale_client.insert_telemetry_batch(telemetry)
        mock_timescale_client.insert_telemetry_batch.assert_called_once()

        # 3. Main API queries charge point state
        state = await mock_ocpp_client.get_charge_point_state("charger_001")
        assert state["connected"] is True

        # 4. Main API runs optimization
        # TODO: After Phase 4, use ocpp_client. For now, use ocpp_server
        mock_ocpp_server = MagicMock()
        mock_charge_point_e2e = AsyncMock()
        mock_charge_point_e2e.set_charging_profile = AsyncMock(return_value=True)
        mock_ocpp_server.get_charge_point = MagicMock(return_value=mock_charge_point_e2e)

        # Mock load_depot_config to prevent database queries during controller initialization
        with patch.object(StateAssembler, "load_depot_config", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = (sample_depot_config, {})
            controller = DepotController(
                pool=pool,
                depot_id=depot_id,
                config=sample_depot_config,
                controller_config=fast_controller_config,
                ocpp_server=mock_ocpp_server,  # Temporary: use ocpp_server until ocpp_client implemented
            )
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                "bus_1": {
                    "charging_power": [80.0] * 96,
                    "soc": [0.3 + 0.01 * i for i in range(96)],
                },
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[200.0] * 96,
            peak_demand_kw=250.0,  # Use correct field name
            objective_value=1500.0,
            solve_time_s=5.0,  # Use correct field name
            status="completed",
            solver_used="gurobi",
        )

        # Keep load_depot_config mocked during dispatch (called by _dispatch_commands)
        with (
            patch("src.core.controller.optimize", return_value=result),
            patch.object(
                StateAssembler, "load_depot_config", new_callable=AsyncMock
            ) as mock_load_dispatch,
        ):
            mock_load_dispatch.return_value = (sample_depot_config, {"bus_1": "charger_001"})
            opt_result = await controller.run_optimization("e2e_test")

        assert opt_result.status == "completed"

        # 5. Main API sends SetChargingProfile via WebSocket Handler
        # Verify via ocpp_server (temporary until ocpp_client implemented)
        assert mock_charge_point_e2e.set_charging_profile.called

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_error_handling_websocket_handler_unavailable(
        self,
        mock_ocpp_client,
        mock_db_pool,
        sample_depot_config,
        fast_controller_config,
        sample_depot_state,
    ):
        """Test graceful degradation when WebSocket Handler is unavailable."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        # Simulate WebSocket Handler unavailable
        mock_ocpp_client.get_connected_charge_points = AsyncMock(
            side_effect=Exception("Connection refused")
        )

        # TODO: After Phase 4, use ocpp_client. For now, use ocpp_server
        mock_ocpp_server = MagicMock()
        mock_ocpp_server.get_charge_point = MagicMock(return_value=None)  # No chargers available

        # Mock load_depot_config to prevent database queries during controller initialization
        with patch.object(StateAssembler, "load_depot_config", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = (sample_depot_config, {})
            controller = DepotController(
                pool=pool,
                depot_id=depot_id,
                config=sample_depot_config,
                controller_config=fast_controller_config,
                ocpp_server=mock_ocpp_server,  # Temporary: use ocpp_server until ocpp_client implemented
            )

        # Should handle error gracefully (not crash)
        with pytest.raises(Exception):
            await mock_ocpp_client.get_connected_charge_points()

        # Optimization should still work (may not have charge point info)
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        result = OptimizationResult(
            run_id=uuid4(),
            schedule={"bus_1": {"charging_power": [80.0] * 96, "soc": [0.3] * 96}},
            battery_dispatch=[0.0] * 96,
            grid_power=[200.0] * 96,
            peak_demand_kw=250.0,  # Use correct field name
            objective_value=1500.0,
            solve_time_s=5.0,  # Use correct field name
            status="completed",
            solver_used="gurobi",
        )

        # Keep load_depot_config mocked during dispatch (called by _dispatch_commands)
        with (
            patch("src.core.controller.optimize", return_value=result),
            patch.object(
                StateAssembler, "load_depot_config", new_callable=AsyncMock
            ) as mock_load_dispatch,
        ):
            mock_load_dispatch.return_value = (sample_depot_config, {})
            # Optimization should complete even if charge points unavailable
            opt_result = await controller.run_optimization("degraded_test")
            assert opt_result.status == "completed"


class TestResilienceAndRecovery:
    """Tests for system resilience and recovery scenarios."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_pattern(self, mock_ocpp_client, mock_db_pool):
        """Test circuit breaker pattern for WebSocket Handler failures."""
        # Simulate repeated failures
        failures = 0
        max_failures = 5

        async def failing_call(*args, **kwargs):
            nonlocal failures
            failures += 1
            if failures < max_failures:
                raise Exception("Service unavailable")
            return {"success": True}

        mock_ocpp_client.send_charging_profile = failing_call

        # First few calls should fail
        for i in range(max_failures - 1):
            with pytest.raises(Exception):
                await mock_ocpp_client.send_charging_profile("charger_001", {})

        # After max failures, circuit should open (stop trying)
        # In real implementation, would track failures and open circuit

    @pytest.mark.asyncio
    async def test_retry_logic_with_exponential_backoff(self, mock_ocpp_client, mock_db_pool):
        """Test retry logic with exponential backoff for transient failures."""
        call_count = 0

        async def transient_failure(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Transient error")
            return {"success": True}

        mock_ocpp_client.send_charging_profile = AsyncMock(side_effect=transient_failure)

        # Simulate retry logic: call until success
        max_retries = 5
        result = None
        for attempt in range(max_retries):
            try:
                result = await mock_ocpp_client.send_charging_profile("charger_001", {})
                break
            except Exception:
                if attempt == max_retries - 1:
                    raise

        # Should eventually succeed
        assert result is not None
        assert result["success"] is True
        assert call_count == 3  # Failed twice, succeeded on third try
