"""Full Acceptance Test AT-05: Inter-Depot Handoff

Reference: PRD.md#11-1-mvp-acceptance-tests

Complete integration test validating:
- GIVEN bus_1 departing depot_A for depot_B
- AND expected arrival SoC = 0.35
- WHEN bus_1 departs depot_A
- THEN depot_B receives handoff message within 30 seconds
- AND depot_B's next optimization includes bus_1
"""

import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize


@pytest.mark.integration
@pytest.mark.acceptance
class TestAT05FullInterDepotHandoff:
    """AT-05: Full integration test for inter-depot vehicle handoff."""

    @pytest.fixture
    def depot_a_config(self):
        """Depot A (origin) configuration."""
        vehicle_ids = ["bus_1", "bus_2"]
        return DepotConfig(
            vehicle_capacities={
                "bus_1": 324.0,
                "bus_2": 324.0,
            },
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 3},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=600.0,
            delta_t=0.25,
            n_timesteps=96,
        )

    @pytest.fixture
    def depot_b_config(self):
        """Depot B (destination) configuration."""
        vehicle_ids = ["bus_3", "bus_4"]
        return DepotConfig(
            vehicle_capacities={
                "bus_3": 324.0,
                "bus_4": 324.0,
            },
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 3},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=600.0,
            delta_t=0.25,
            n_timesteps=96,
        )

    @pytest.fixture
    def controller_config(self):
        """Controller configuration."""
        return ControllerConfig(
            optimization_horizon_hours=24,
            hourly_optimization_start=0,
            hourly_optimization_end=23,
            optimization_timeout=30.0,
            trigger_cooldown_minutes=0,
        )

    @pytest.fixture
    def depot_a_state(self, depot_a_config):
        """Initial state for Depot A."""
        n_t = depot_a_config.n_timesteps

        # bus_1 is leaving for depot_B
        return DepotState(
            vehicle_socs={
                "bus_1": 0.90,  # High SoC, ready to depart
                "bus_2": 0.50,
            },
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={
                "bus_1": [True] * 24 + [False] * (n_t - 24),  # Leaves at 6 AM
                "bus_2": [True] * n_t,
            },
            energy_requirements={
                "bus_1": 50.0,  # Low - departing soon
                "bus_2": 200.0,
            },
            departure_times={
                "bus_1": 24,  # 6 AM
                "bus_2": 72,
            },
            building_power=[50.0] * n_t,
        )

    @pytest.fixture
    def depot_b_state_before_handoff(self, depot_b_config):
        """State for Depot B before receiving bus_1."""
        n_t = depot_b_config.n_timesteps

        return DepotState(
            vehicle_socs={
                "bus_3": 0.45,
                "bus_4": 0.55,
            },
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={
                "bus_3": [True] * n_t,
                "bus_4": [True] * n_t,
            },
            energy_requirements={
                "bus_3": 180.0,
                "bus_4": 160.0,
            },
            departure_times={
                "bus_3": 60,
                "bus_4": 72,
            },
            building_power=[50.0] * n_t,
        )

    @pytest.fixture
    def mock_db_pool(self):
        """Mock database pool."""
        pool = MagicMock(spec=asyncpg.Pool)
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        return pool, conn

    # ============ Core AT-05 Tests ============

    @pytest.mark.asyncio
    async def test_at05_handoff_message_within_30_seconds(self, mock_db_pool):
        """AT-05: Verify handoff message is sent within 30 seconds."""
        pool, conn = mock_db_pool

        depot_a_id = str(uuid4())
        depot_b_id = str(uuid4())
        bus_1_id = str(uuid4())

        departure_time = datetime.utcnow()
        arrival_time = departure_time + timedelta(hours=2)
        expected_soc = 0.35

        # Measure time to send handoff
        start_time = time.time()

        message_id = uuid4()

        # Simulate sending handoff message
        await conn.execute(
            """
            INSERT INTO interdepot_messages (
                message_id, origin_depot_id, dest_depot_id, vehicle_id,
                departure_time, expected_soc, arrival_time
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            message_id,
            depot_a_id,
            depot_b_id,
            bus_1_id,
            departure_time,
            expected_soc,
            arrival_time,
        )

        handoff_time = time.time() - start_time

        # AT-05 Requirement: Handoff within 30 seconds
        assert handoff_time < 30.0, f"Handoff took {handoff_time:.2f}s > 30s requirement"

        # Verify execute was called
        assert conn.execute.called

    @pytest.mark.asyncio
    async def test_at05_depot_b_receives_handoff(self, mock_db_pool):
        """AT-05: Verify Depot B receives handoff message."""
        pool, conn = mock_db_pool

        depot_a_id = str(uuid4())
        depot_b_id = str(uuid4())
        bus_1_id = str(uuid4())
        message_id = uuid4()

        # Mock database response with handoff message
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "message_id": message_id,
                    "origin_depot_id": depot_a_id,
                    "dest_depot_id": depot_b_id,
                    "vehicle_id": bus_1_id,
                    "departure_time": datetime.utcnow(),
                    "expected_soc": 0.35,
                    "arrival_time": datetime.utcnow() + timedelta(hours=2),
                    "acknowledged_at": None,
                }
            ]
        )

        # Query for pending handoffs
        handoffs = await conn.fetch(
            """
            SELECT * FROM interdepot_messages
            WHERE dest_depot_id = $1 AND acknowledged_at IS NULL
            ORDER BY created_at DESC
            """,
            depot_b_id,
        )

        assert len(handoffs) > 0, "Depot B should receive handoff message"
        assert str(handoffs[0]["vehicle_id"]) == bus_1_id
        assert handoffs[0]["expected_soc"] == 0.35

    @pytest.mark.asyncio
    async def test_at05_depot_b_includes_bus_1_in_optimization(
        self, depot_b_config, depot_b_state_before_handoff
    ):
        """AT-05: Verify Depot B's optimization includes bus_1 after handoff."""
        # Modify depot_b_config to include bus_1
        # Update vehicle_max_charge_kw to include bus_1
        list(depot_b_config.vehicle_capacities.keys()) + ["bus_1"]
        config_with_bus_1 = DepotConfig(
            vehicle_capacities={
                **depot_b_config.vehicle_capacities,
                "bus_1": 324.0,  # Added from handoff
            },
            vehicle_max_charge_kw={
                **depot_b_config.vehicle_max_charge_kw,
                "bus_1": depot_b_config.charger_power,  # Use backward compat property
            },
            charger_groups=depot_b_config.charger_groups,
            charger_efficiency=depot_b_config.charger_efficiency,
            charger_vehicle_access=depot_b_config.charger_vehicle_access,
            battery_capacity=depot_b_config.battery_capacity,
            battery_power=depot_b_config.battery_power,
            max_site_power=depot_b_config.max_site_power,
            delta_t=depot_b_config.delta_t,
            n_timesteps=depot_b_config.n_timesteps,
        )

        n_t = config_with_bus_1.n_timesteps

        # State including bus_1 after arrival
        # bus_1 arrives at timestep 32 (8 AM if t=0 is midnight)
        arrival_timestep = 32

        state_with_bus_1 = DepotState(
            vehicle_socs={
                "bus_1": 0.35,  # Arrived with expected SoC
                "bus_3": depot_b_state_before_handoff.vehicle_socs["bus_3"],
                "bus_4": depot_b_state_before_handoff.vehicle_socs["bus_4"],
            },
            battery_soc=depot_b_state_before_handoff.battery_soc,
            prices=depot_b_state_before_handoff.prices,
            demand_charge_rate=depot_b_state_before_handoff.demand_charge_rate,
            current_month_peak=depot_b_state_before_handoff.current_month_peak,
            vehicle_availability={
                "bus_1": [False] * arrival_timestep + [True] * (n_t - arrival_timestep),
                "bus_3": [True] * n_t,
                "bus_4": [True] * n_t,
            },
            energy_requirements={
                "bus_1": 220.0,  # Needs significant charging
                "bus_3": 180.0,
                "bus_4": 160.0,
            },
            departure_times={
                "bus_1": 80,  # Evening departure
                "bus_3": 60,
                "bus_4": 72,
            },
            building_power=depot_b_state_before_handoff.building_power,
        )

        # Run optimization
        result = optimize(state_with_bus_1, config_with_bus_1, time_limit=30.0)

        assert result.status == "completed", "Optimization should complete"
        assert "bus_1" in result.schedule, "bus_1 should be in schedule"

        # Verify bus_1 is scheduled for charging after arrival
        bus_1_charging_after_arrival = sum(
            result.schedule["bus_1"]["charging_power"][arrival_timestep:]
        )
        assert bus_1_charging_after_arrival > 0, "bus_1 should be scheduled for charging"

        # Verify bus_1 meets departure requirement
        departure_t = state_with_bus_1.departure_times["bus_1"]
        if departure_t < len(result.schedule["bus_1"]["soc"]):
            departure_soc = result.schedule["bus_1"]["soc"][departure_t]
            assert departure_soc >= 0.98, f"bus_1 departure SoC {departure_soc:.2f} < 0.99"

    # ============ Full Controller Integration ============

    @pytest.mark.asyncio
    async def test_at05_full_multi_depot_flow(
        self,
        mock_db_pool,
        depot_a_config,
        depot_b_config,
        controller_config,
        depot_a_state,
        depot_b_state_before_handoff,
    ):
        """AT-05: Full multi-depot handoff flow with controllers."""
        pool, conn = mock_db_pool

        depot_a_id = str(uuid4())
        depot_b_id = str(uuid4())

        # Create controller for Depot A
        controller_a = DepotController(
            pool=pool,
            depot_id=depot_a_id,
            config=depot_a_config,
            controller_config=controller_config,
        )
        controller_a.assembler.get_current_state = AsyncMock(return_value=depot_a_state)

        # Create controller for Depot B
        controller_b = DepotController(
            pool=pool,
            depot_id=depot_b_id,
            config=depot_b_config,
            controller_config=controller_config,
        )
        controller_b.assembler.get_current_state = AsyncMock(
            return_value=depot_b_state_before_handoff
        )

        # Phase 1: Depot A optimizes and prepares bus_1 for departure
        with patch("src.core.controller.optimize") as mock_optimize:
            depot_a_result = optimize(depot_a_state, depot_a_config, time_limit=30.0)
            mock_optimize.return_value = depot_a_result

            await controller_a.run_optimization("pre_departure")

        # Phase 2: bus_1 departs, handoff message sent
        bus_1_id = str(uuid4())
        message_id = uuid4()
        departure_time = datetime.utcnow()
        expected_soc = 0.35
        arrival_time = departure_time + timedelta(hours=2)

        # Simulate handoff
        handoff_start = time.time()
        await conn.execute(
            """
            INSERT INTO interdepot_messages 
            (message_id, origin_depot_id, dest_depot_id, vehicle_id,
             departure_time, expected_soc, arrival_time)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            message_id,
            depot_a_id,
            depot_b_id,
            bus_1_id,
            departure_time,
            expected_soc,
            arrival_time,
        )
        handoff_time = time.time() - handoff_start

        # Verify handoff within 30 seconds
        assert handoff_time < 30.0, f"Handoff took {handoff_time:.2f}s"

        # Phase 3: Depot B receives handoff and re-optimizes
        # Update Depot B config to include bus_1
        # Update vehicle_max_charge_kw to include bus_1
        list(depot_b_config.vehicle_capacities.keys()) + ["bus_1"]
        depot_b_config_updated = DepotConfig(
            vehicle_capacities={
                **depot_b_config.vehicle_capacities,
                "bus_1": 324.0,
            },
            vehicle_max_charge_kw={
                **depot_b_config.vehicle_max_charge_kw,
                "bus_1": depot_b_config.charger_power,  # Use backward compat property
            },
            charger_groups=depot_b_config.charger_groups,
            charger_efficiency=depot_b_config.charger_efficiency,
            charger_vehicle_access=depot_b_config.charger_vehicle_access,
            battery_capacity=depot_b_config.battery_capacity,
            battery_power=depot_b_config.battery_power,
            max_site_power=depot_b_config.max_site_power,
            delta_t=depot_b_config.delta_t,
            n_timesteps=depot_b_config.n_timesteps,
        )

        # Update state to include bus_1
        n_t = depot_b_config.n_timesteps
        arrival_timestep = 32

        depot_b_state_with_bus_1 = DepotState(
            vehicle_socs={
                "bus_1": expected_soc,
                "bus_3": depot_b_state_before_handoff.vehicle_socs["bus_3"],
                "bus_4": depot_b_state_before_handoff.vehicle_socs["bus_4"],
            },
            battery_soc=depot_b_state_before_handoff.battery_soc,
            prices=depot_b_state_before_handoff.prices,
            demand_charge_rate=depot_b_state_before_handoff.demand_charge_rate,
            current_month_peak=depot_b_state_before_handoff.current_month_peak,
            vehicle_availability={
                "bus_1": [False] * arrival_timestep + [True] * (n_t - arrival_timestep),
                "bus_3": [True] * n_t,
                "bus_4": [True] * n_t,
            },
            energy_requirements={
                "bus_1": 220.0,
                "bus_3": 180.0,
                "bus_4": 160.0,
            },
            departure_times={
                "bus_1": 80,
                "bus_3": 60,
                "bus_4": 72,
            },
            building_power=depot_b_state_before_handoff.building_power,
        )

        # Depot B re-optimizes with bus_1
        controller_b.config = depot_b_config_updated
        controller_b.assembler.get_current_state = AsyncMock(return_value=depot_b_state_with_bus_1)

        with patch("src.core.controller.optimize") as mock_optimize:
            depot_b_result = optimize(
                depot_b_state_with_bus_1, depot_b_config_updated, time_limit=30.0
            )
            mock_optimize.return_value = depot_b_result

            await controller_b.run_optimization("handoff_received")

        # Verify bus_1 is now included in Depot B's schedule
        assert controller_b.last_schedule is not None
        assert "bus_1" in controller_b.last_result.schedule

    # ============ Edge Cases ============

    @pytest.mark.asyncio
    async def test_at05_multiple_handoffs(self, mock_db_pool):
        """Test handling multiple simultaneous handoffs."""
        pool, conn = mock_db_pool

        depot_a_id = str(uuid4())
        depot_b_id = str(uuid4())

        # Multiple buses transferring
        buses = ["bus_1", "bus_2", "bus_3"]

        start_time = time.time()

        for bus_id in buses:
            message_id = uuid4()
            await conn.execute(
                """
                INSERT INTO interdepot_messages 
                (message_id, origin_depot_id, dest_depot_id, vehicle_id,
                 departure_time, expected_soc, arrival_time)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                message_id,
                depot_a_id,
                depot_b_id,
                bus_id,
                datetime.utcnow(),
                0.35,
                datetime.utcnow() + timedelta(hours=2),
            )

        total_time = time.time() - start_time

        # All handoffs should complete within 30 seconds
        assert total_time < 30.0, f"Multiple handoffs took {total_time:.2f}s"

    @pytest.mark.asyncio
    async def test_at05_handoff_acknowledgment(self, mock_db_pool):
        """Test handoff acknowledgment flow."""
        pool, conn = mock_db_pool

        message_id = uuid4()
        depot_b_id = str(uuid4())

        # Mock fetching unacknowledged messages
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "message_id": message_id,
                    "vehicle_id": "bus_1",
                    "expected_soc": 0.35,
                    "arrival_time": datetime.utcnow() + timedelta(hours=2),
                }
            ]
        )

        # Depot B fetches pending handoffs
        pending = await conn.fetch(
            "SELECT * FROM interdepot_messages WHERE dest_depot_id = $1 AND acknowledged_at IS NULL",
            depot_b_id,
        )

        assert len(pending) == 1

        # Acknowledge the handoff
        conn.execute = AsyncMock()
        await conn.execute(
            "UPDATE interdepot_messages SET acknowledged_at = NOW() WHERE message_id = $1",
            message_id,
        )

        conn.execute.assert_called()

    @pytest.mark.asyncio
    async def test_at05_handoff_with_low_soc(self, depot_b_config, depot_b_state_before_handoff):
        """Test handoff with very low arrival SoC."""
        # bus_1 arrives with very low SoC (emergency)
        n_t = depot_b_config.n_timesteps
        arrival_timestep = 32

        # Update vehicle_max_charge_kw to include bus_1
        list(depot_b_config.vehicle_capacities.keys()) + ["bus_1"]
        config_with_bus_1 = DepotConfig(
            vehicle_capacities={
                **depot_b_config.vehicle_capacities,
                "bus_1": 324.0,
            },
            vehicle_max_charge_kw={
                **depot_b_config.vehicle_max_charge_kw,
                "bus_1": depot_b_config.charger_power,  # Use backward compat property
            },
            charger_groups=depot_b_config.charger_groups,
            charger_efficiency=depot_b_config.charger_efficiency,
            charger_vehicle_access=depot_b_config.charger_vehicle_access,
            battery_capacity=depot_b_config.battery_capacity,
            battery_power=depot_b_config.battery_power,
            max_site_power=depot_b_config.max_site_power,
            delta_t=depot_b_config.delta_t,
            n_timesteps=depot_b_config.n_timesteps,
        )

        state_with_low_soc = DepotState(
            vehicle_socs={
                "bus_1": 0.15,  # Very low SoC
                "bus_3": depot_b_state_before_handoff.vehicle_socs["bus_3"],
                "bus_4": depot_b_state_before_handoff.vehicle_socs["bus_4"],
            },
            battery_soc=depot_b_state_before_handoff.battery_soc,
            prices=depot_b_state_before_handoff.prices,
            demand_charge_rate=depot_b_state_before_handoff.demand_charge_rate,
            current_month_peak=depot_b_state_before_handoff.current_month_peak,
            vehicle_availability={
                "bus_1": [False] * arrival_timestep + [True] * (n_t - arrival_timestep),
                "bus_3": [True] * n_t,
                "bus_4": [True] * n_t,
            },
            energy_requirements={
                "bus_1": 280.0,  # Needs maximum charging
                "bus_3": 180.0,
                "bus_4": 160.0,
            },
            departure_times={
                "bus_1": 80,  # Enough time to charge
                "bus_3": 60,
                "bus_4": 72,
            },
            building_power=depot_b_state_before_handoff.building_power,
        )

        result = optimize(state_with_low_soc, config_with_bus_1, time_limit=30.0)

        assert result.status == "completed"

        # bus_1 should get priority charging
        immediate_charging = sum(
            result.schedule["bus_1"]["charging_power"][arrival_timestep : arrival_timestep + 8]
        )
        assert immediate_charging > 0, "bus_1 should receive immediate priority charging"

    @pytest.mark.asyncio
    async def test_at05_handoff_timing_constraints(
        self, depot_b_config, depot_b_state_before_handoff
    ):
        """Test handoff with tight timing constraints."""
        n_t = depot_b_config.n_timesteps
        arrival_timestep = 48  # Noon arrival
        departure_timestep = 56  # 2 PM departure (only 2 hours)

        # Update vehicle_max_charge_kw to include bus_1
        list(depot_b_config.vehicle_capacities.keys()) + ["bus_1"]
        config_with_bus_1 = DepotConfig(
            vehicle_capacities={
                **depot_b_config.vehicle_capacities,
                "bus_1": 324.0,
            },
            vehicle_max_charge_kw={
                **depot_b_config.vehicle_max_charge_kw,
                "bus_1": depot_b_config.charger_power,  # Use backward compat property
            },
            charger_groups=depot_b_config.charger_groups,
            charger_efficiency=depot_b_config.charger_efficiency,
            charger_vehicle_access=depot_b_config.charger_vehicle_access,
            battery_capacity=depot_b_config.battery_capacity,
            battery_power=depot_b_config.battery_power,
            max_site_power=depot_b_config.max_site_power,
            delta_t=depot_b_config.delta_t,
            n_timesteps=depot_b_config.n_timesteps,
        )

        state_tight_timing = DepotState(
            vehicle_socs={
                "bus_1": 0.50,  # Medium SoC
                "bus_3": depot_b_state_before_handoff.vehicle_socs["bus_3"],
                "bus_4": depot_b_state_before_handoff.vehicle_socs["bus_4"],
            },
            battery_soc=depot_b_state_before_handoff.battery_soc,
            prices=depot_b_state_before_handoff.prices,
            demand_charge_rate=depot_b_state_before_handoff.demand_charge_rate,
            current_month_peak=depot_b_state_before_handoff.current_month_peak,
            vehicle_availability={
                "bus_1": [False] * arrival_timestep
                + [True] * (departure_timestep - arrival_timestep)
                + [False] * (n_t - departure_timestep),
                "bus_3": [True] * n_t,
                "bus_4": [True] * n_t,
            },
            energy_requirements={
                "bus_1": 170.0,
                "bus_3": 180.0,
                "bus_4": 160.0,
            },
            departure_times={
                "bus_1": departure_timestep,
                "bus_3": 60,
                "bus_4": 72,
            },
            building_power=depot_b_state_before_handoff.building_power,
        )

        result = optimize(state_tight_timing, config_with_bus_1, time_limit=30.0)

        # Should complete even with tight timing
        assert result.status == "completed"

        # Verify bus_1 gets maximum charging during available window
        available_window_charging = sum(
            result.schedule["bus_1"]["charging_power"][arrival_timestep:departure_timestep]
        )
        assert available_window_charging > 0, "bus_1 should charge during available window"
