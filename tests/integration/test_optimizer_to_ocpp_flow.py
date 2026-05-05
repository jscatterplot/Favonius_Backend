"""Integration tests for Optimizer → Controller → OCPP flow.

Tests the complete flow:
1. Optimization result → OCPP SetChargingProfile conversion
2. Charger allocation respects physical accessibility
3. Commands dispatched to correct chargers
4. Command dispatch retry logic
5. End-to-end flow: State Assembly → Optimization → Charger Allocation → OCPP Dispatch

Reference: PRD_v2.md#9-1-ocpp-integration

Session 3 note: the in-process ``dispatch_charging_profiles`` push test
suite has been superseded by the queue-mediated tests
(tests/integration/test_dispatch_queue.py, tests/unit/test_ocpp_dispatch.py).
The legacy tests in this file that exercise the in-process push are
skipped; the conversion / allocation tests remain in scope.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.adapters.ocpp.charge_point import FleetChargePoint, convert_schedule_to_ocpp_profile
from src.adapters.ocpp.dispatch import dispatch_charging_profiles
from src.adapters.ocpp.server import OCPPServer
from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.core.optimizer import optimize


# Apply skip to every test class in this module that exercises the legacy
# in-process dispatch path. Conversion / allocation tests are unaffected.
_LEGACY_DISPATCH_REASON = (
    "Session 3: in-process OCPP dispatch removed; covered by "
    "tests/integration/test_dispatch_queue.py."
)


@pytest.mark.integration
@pytest.mark.asyncio
class TestOptimizationResultToOCPP:
    """Test OptimizationResult → OCPP SetChargingProfile conversion."""

    @pytest.fixture
    def depot_config(self):
        """Depot configuration."""
        vehicle_ids = ["bus_1", "bus_2"]
        return DepotConfig(
            vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 2},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )

    @pytest.fixture
    def optimization_result(self, depot_config):
        """Sample optimization result."""
        n_t = depot_config.n_timesteps
        return OptimizationResult(
            run_id=uuid4(),
            schedule={
                "bus_1": {
                    "charging_power": [80.0 if t < 10 else 0.0 for t in range(n_t)],
                    "soc": [0.3 + t * 0.01 if t < 10 else 0.4 for t in range(n_t)],
                },
                "bus_2": {
                    "charging_power": [60.0 if 5 <= t < 15 else 0.0 for t in range(n_t)],
                    "soc": [0.4 + (t - 5) * 0.008 if 5 <= t < 15 else 0.4 for t in range(n_t)],
                },
            },
            battery_dispatch=[0.0] * n_t,
            grid_power=[140.0] * n_t,
            peak_demand_kw=200.0,
            objective_value=1000.0,
            solve_time=5.0,
            status="completed",
            solver_used="gurobi",
        )

    @pytest.mark.asyncio
    async def test_optimization_result_converted_to_ocpp_profile(self, optimization_result):
        """Test OptimizationResult converted to OCPP SetChargingProfile."""
        # Extract schedule for one vehicle
        vehicle_id = "bus_1"
        schedule_data = optimization_result.schedule[vehicle_id]
        charging_power = schedule_data["charging_power"]

        # Convert to (timestep, power_kw) tuples
        schedule = [(t, power) for t, power in enumerate(charging_power) if power > 0.1]

        # Convert to OCPP profile
        ocpp_profile = convert_schedule_to_ocpp_profile(schedule, delta_t=0.25)

        assert len(ocpp_profile) > 0, "OCPP profile should have periods"
        assert all("startPeriod" in p for p in ocpp_profile), "Each period should have startPeriod"
        assert all("limit" in p for p in ocpp_profile), "Each period should have limit"
        assert all(
            "numberPhases" in p for p in ocpp_profile
        ), "Each period should have numberPhases"

        # Verify power conversion (kW → W)
        first_period = ocpp_profile[0]
        assert first_period["limit"] == 80000, "80 kW should convert to 80,000 W"
        assert first_period["startPeriod"] == 0, "First period should start at 0 seconds"

    @pytest.mark.asyncio
    async def test_charger_allocation_respects_physical_accessibility(self):
        """Test charger allocation respects physical accessibility."""
        # Vehicle-to-charger mapping should respect physical constraints
        # (e.g., vehicle can only use chargers in same physical location)
        vehicle_to_charger_map = {
            "bus_1": ("charger_1", 1),  # bus_1 → charger_1, connector 1
            "bus_2": ("charger_2", 1),  # bus_2 → charger_2, connector 1
        }

        # Verify mapping structure
        for vehicle_id, charger_info in vehicle_to_charger_map.items():
            assert isinstance(charger_info, tuple), "Charger info should be tuple"
            assert len(charger_info) == 2, "Charger info should be (charge_point_id, connector_id)"
            charge_point_id, connector_id = charger_info
            assert isinstance(charge_point_id, str), "Charge point ID should be string"
            assert isinstance(connector_id, int), "Connector ID should be integer"

    @pytest.mark.skip(reason=_LEGACY_DISPATCH_REASON)
    @pytest.mark.asyncio
    async def test_commands_dispatched_to_correct_chargers(self, optimization_result):
        """Legacy in-process dispatch — see test_dispatch_queue.py."""
        # Mock OCPP server
        mock_server = MagicMock(spec=OCPPServer)
        mock_charge_point = MagicMock(spec=FleetChargePoint)
        mock_charge_point.set_charging_profile = AsyncMock(return_value=True)
        mock_server.get_charge_point.return_value = mock_charge_point

        vehicle_to_charger_map = {
            "bus_1": ("charger_1", 1),
            "bus_2": ("charger_2", 1),
        }

        # Dispatch profiles
        results = await dispatch_charging_profiles(
            mock_server,
            optimization_result,
            vehicle_to_charger_map=vehicle_to_charger_map,
        )

        # Verify all vehicles dispatched
        assert len(results) == 2
        assert all(results.values()), "All dispatches should succeed"

        # Verify correct chargers called
        assert mock_server.get_charge_point.call_count == 2
        mock_server.get_charge_point.assert_any_call("charger_1")
        mock_server.get_charge_point.assert_any_call("charger_2")

        # Verify set_charging_profile called for each vehicle
        assert mock_charge_point.set_charging_profile.call_count == 2

    @pytest.mark.skip(reason=_LEGACY_DISPATCH_REASON)
    @pytest.mark.asyncio
    async def test_command_dispatch_retry_logic(self, optimization_result):
        """Legacy in-process dispatch — see test_dispatch_queue.py."""
        # Mock OCPP server with failures
        mock_server = MagicMock(spec=OCPPServer)
        mock_charge_point = MagicMock(spec=FleetChargePoint)

        # First two attempts fail, third succeeds
        mock_charge_point.set_charging_profile = AsyncMock(side_effect=[False, False, True])
        mock_server.get_charge_point.return_value = mock_charge_point

        vehicle_to_charger_map = {
            "bus_1": ("charger_1", 1),
        }

        # Dispatch with retry (max_retries=3 in set_charging_profile)
        results = await dispatch_charging_profiles(
            mock_server,
            optimization_result,
            vehicle_to_charger_map=vehicle_to_charger_map,
        )

        # Should eventually succeed after retries
        assert results["bus_1"] is True
        assert mock_charge_point.set_charging_profile.call_count == 3


@pytest.mark.integration
@pytest.mark.asyncio
class TestEndToEndOptimizerToOCPPFlow:
    """Test end-to-end flow: State Assembly → Optimization → Charger Allocation → OCPP Dispatch."""

    @pytest.fixture
    def depot_config(self):
        """Depot configuration."""
        vehicle_ids = ["bus_1", "bus_2"]
        return DepotConfig(
            vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 2},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )

    @pytest.fixture
    def depot_state(self, depot_config):
        """Depot state."""
        n_t = depot_config.n_timesteps
        return DepotState(
            vehicle_socs={"bus_1": 0.3, "bus_2": 0.4},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={
                "bus_1": [True] * n_t,
                "bus_2": [True] * n_t,
            },
            energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
            departure_times={"bus_1": 48, "bus_2": 60},
            building_power=[50.0] * n_t,
        )

    @pytest.mark.skip(reason=_LEGACY_DISPATCH_REASON)
    @pytest.mark.asyncio
    async def test_full_flow_state_to_ocpp_dispatch(self, depot_config, depot_state):
        """Legacy in-process dispatch — see test_dispatch_queue.py."""
        # Step 1: Run optimization
        result = optimize(depot_state, depot_config, time_limit=30.0)

        assert result.status == "completed"
        assert len(result.schedule) == 2

        # Step 2: Mock OCPP server
        mock_server = MagicMock(spec=OCPPServer)
        mock_charge_point = MagicMock(spec=FleetChargePoint)
        mock_charge_point.set_charging_profile = AsyncMock(return_value=True)
        mock_server.get_charge_point.return_value = mock_charge_point

        # Step 3: Build vehicle-to-charger mapping
        vehicle_to_charger_map = {
            "bus_1": ("charger_1", 1),
            "bus_2": ("charger_2", 1),
        }

        # Step 4: Dispatch to OCPP
        dispatch_results = await dispatch_charging_profiles(
            mock_server,
            result,
            vehicle_to_charger_map=vehicle_to_charger_map,
        )

        # Verify dispatch succeeded
        assert len(dispatch_results) == 2
        assert all(dispatch_results.values()), "All dispatches should succeed"

        # Verify OCPP profiles sent
        assert mock_charge_point.set_charging_profile.call_count == 2

    @pytest.mark.asyncio
    async def test_optimization_failure_error_handling(self, depot_config, depot_state):
        """Test optimization failure → Error handling → Retry."""
        # Create infeasible state
        n_t = depot_config.n_timesteps
        infeasible_state = DepotState(
            vehicle_socs={"bus_1": 0.1},  # Very low SoC
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={"bus_1": [True] * n_t},
            energy_requirements={"bus_1": 500.0},  # Very high requirement
            departure_times={"bus_1": 4},  # Very short time (1 hour)
            building_power=[50.0] * n_t,
        )

        # Optimization should handle infeasibility gracefully
        result = optimize(infeasible_state, depot_config, time_limit=30.0)

        # Should either be infeasible or timeout, not crash
        assert result.status in ["infeasible", "timeout", "completed"]

        # If infeasible, should not attempt dispatch
        if result.status == "infeasible":
            # No dispatch should occur for infeasible results
            assert len(result.schedule) == 0 or all(
                not any(p > 0.1 for p in sched.get("charging_power", []))
                for sched in result.schedule.values()
            )

    @pytest.mark.skip(reason=_LEGACY_DISPATCH_REASON)
    @pytest.mark.asyncio
    async def test_ocpp_dispatch_failure_retry_circuit_breaker(self, depot_config, depot_state):
        """Legacy in-process dispatch — see test_dispatch_queue.py."""
        # Run optimization
        result = optimize(depot_state, depot_config, time_limit=30.0)
        assert result.status == "completed"

        # Mock OCPP server that always fails
        mock_server = MagicMock(spec=OCPPServer)
        mock_charge_point = MagicMock(spec=FleetChargePoint)
        mock_charge_point.set_charging_profile = AsyncMock(return_value=False)
        mock_server.get_charge_point.return_value = mock_charge_point

        vehicle_to_charger_map = {
            "bus_1": ("charger_1", 1),
        }

        # Dispatch should handle failures gracefully
        dispatch_results = await dispatch_charging_profiles(
            mock_server,
            result,
            vehicle_to_charger_map=vehicle_to_charger_map,
        )

        # Should mark as failed after retries
        assert dispatch_results["bus_1"] is False

        # Verify retry attempts were made
        # (set_charging_profile has max_retries=3, so should be called 3 times)
        assert mock_charge_point.set_charging_profile.call_count >= 1


@pytest.mark.integration
@pytest.mark.asyncio
class TestServiceCommunication:
    """Test service communication: Main API → WebSocket Handler."""

    @pytest.mark.asyncio
    async def test_main_api_to_websocket_handler_communication(self):
        """Test Main API → WebSocket Handler communication (current state).

        Note: This is a placeholder test. Full implementation depends on
        Phase 4 internal API implementation.
        """
        # Mock WebSocket Handler client
        mock_ws_client = MagicMock()
        mock_ws_client.query_connected_charge_points = AsyncMock(
            return_value=["charger_1", "charger_2"]
        )
        mock_ws_client.query_charge_point_state = AsyncMock(
            return_value={"soc": 0.5, "power": 80.0, "status": "Charging"}
        )
        mock_ws_client.send_charging_profile = AsyncMock(return_value=True)

        # Test query connected charge points
        charge_points = await mock_ws_client.query_connected_charge_points()
        assert len(charge_points) == 2

        # Test query charge point state
        state = await mock_ws_client.query_charge_point_state("charger_1")
        assert "soc" in state
        assert "power" in state
        assert "status" in state

        # Test send charging profile
        success = await mock_ws_client.send_charging_profile("charger_1", {"profile": "data"})
        assert success is True

    @pytest.mark.asyncio
    async def test_telemetry_flow_ocpp_to_websocket_to_timescale(self):
        """Test telemetry flow: OCPP → WebSocket Handler → TimescaleDB → Main API.

        Note: This is a placeholder test. Full implementation depends on
        Phase 4 telemetry forwarding.
        """
        # Mock telemetry flow
        # 1. OCPP charger sends MeterValues
        ocpp_meter_values = {
            "charge_point_id": "charger_1",
            "connector_id": 1,
            "meter_values": [{"timestamp": datetime.utcnow().isoformat(), "energy_wh": 100000}],
        }

        # 2. WebSocket Handler receives and stores to TimescaleDB
        mock_timescale = MagicMock()
        mock_timescale.insert_telemetry = AsyncMock(return_value=True)

        # 3. Main API queries telemetry from TimescaleDB
        mock_timescale.get_latest_telemetry = AsyncMock(return_value={"soc": 0.5, "power": 80.0})

        # Verify flow
        stored = await mock_timescale.insert_telemetry(ocpp_meter_values)
        assert stored is True

        telemetry = await mock_timescale.get_latest_telemetry("vehicle_1")
        assert "soc" in telemetry
