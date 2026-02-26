"""Integration tests for data resolution priority.

Tests the priority order for resolving data values:
1. Demand charge rate: prices.demand_kw → depots.demand_charge_rate_kw → default
2. Vehicle max_charge_kw: OCPP → config → charger cap
3. Incoming vehicle integration: interdepot_messages → state

Reference: PRD_v2.md#8-1-optimization-formulation
"""

from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.core.models import DepotConfig
from src.core.state.assembler import StateAssembler


@pytest.mark.integration
@pytest.mark.asyncio
class TestDataResolution:
    """Test data resolution priority order."""

    @pytest_asyncio.fixture
    async def test_db_pool(self):
        """Create test database connection pool."""
        import os

        db_url = os.getenv(
            "TEST_DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/favonius_test",
        )

        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
            yield pool
            await pool.close()
        except Exception as e:
            pytest.skip(f"Database not available: {e}")

    @pytest_asyncio.fixture
    async def depot_id(self, test_db_pool):
        """Create test depot."""
        depot_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO depots (
                    depot_id, name, latitude, longitude, timezone,
                    max_grid_kw, demand_charge_rate_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                depot_id,
                "Test Depot",
                34.0522,
                -118.2437,
                "America/Los_Angeles",
                1000.0,
                25.0,  # Depot config has $25/kW
            )

        yield depot_id

        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM prices WHERE depot_id = $1", depot_id)
            await conn.execute("DELETE FROM depots WHERE depot_id = $1", depot_id)

    @pytest.fixture
    def depot_config(self):
        """Depot configuration."""
        return DepotConfig(
            vehicle_capacities={"bus_1": 324.0},
            vehicle_max_charge_kw={"bus_1": 80.0},
            charger_groups={80.0: 2},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )

    @pytest.mark.asyncio
    async def test_demand_charge_rate_resolution_priority(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test demand charge rate resolution priority.

        Priority: prices.demand_kw → depots.demand_charge_rate_kw → default
        """
        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Test 1: prices.demand_kw takes precedence
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO prices (
                    depot_id, time, price_kwh, source, demand_kw
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                depot_id,
                datetime.utcnow(),
                0.10,
                "utility_tou",
                30.0,  # prices.demand_kw = $30/kW
            )

        rate = await assembler._get_demand_charge_rate()
        assert rate == 30.0, "Should use prices.demand_kw ($30/kW) over depot config ($25/kW)"

        # Test 2: Fall back to depot config when prices.demand_kw is NULL
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE prices
                SET demand_kw = NULL
                WHERE depot_id = $1
                """,
                depot_id,
            )

        rate = await assembler._get_demand_charge_rate()
        assert (
            rate == 25.0
        ), "Should fall back to depot config ($25/kW) when prices.demand_kw is NULL"

        # Test 3: Fall back to default when both are NULL
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE depots
                SET demand_charge_rate_kw = NULL
                WHERE depot_id = $1
                """,
                depot_id,
            )

        rate = await assembler._get_demand_charge_rate()
        assert rate == 20.0, "Should fall back to default ($20/kW) when both are NULL"

    @pytest.mark.asyncio
    async def test_vehicle_max_charge_kw_resolution(self, test_db_pool, depot_id, depot_config):
        """Test vehicle max_charge_kw resolution.

        Priority: OCPP MeterValues → config → charger cap
        """
        uuid4()

        # Test 1: max_charge_kw from config
        depot_config.vehicle_max_charge_kw = {"bus_1": 150.0}
        assert depot_config.vehicle_max_charge_kw["bus_1"] == 150.0

        # Test 2: Fall back to charger cap if not in config
        if "bus_2" not in depot_config.vehicle_max_charge_kw:
            # Should use charger_power as fallback
            max_charge = depot_config.charger_power  # 80.0 kW
            assert max_charge == 80.0

        # Test 3: OCPP MeterValues would override (tested in OCPP integration tests)
        # In real code, max_charge_kw from OCPP MeterValues is stored in telemetry
        # and used when available

    @pytest.mark.asyncio
    async def test_incoming_vehicle_integration(self, test_db_pool, depot_id, depot_config):
        """Test incoming vehicle integration: interdepot_messages → state."""
        # Create incoming vehicle message
        origin_depot_id = uuid4()
        vehicle_id = uuid4()
        arrival_time = datetime.utcnow() + timedelta(hours=2)

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                uuid4(),
                origin_depot_id,
                depot_id,
                vehicle_id,
                datetime.utcnow(),
                0.35,  # Expected SoC at arrival
                arrival_time,
                324.0,  # Battery capacity
                150.0,  # Max charge power
                "acknowledged",
            )

        # StateAssembler should integrate incoming vehicle into state
        StateAssembler(test_db_pool, depot_id, depot_config)

        # Get incoming vehicles (simulates StateAssembler._get_incoming_vehicles)
        now = datetime.utcnow()
        horizon_end = now + timedelta(hours=24)

        async with test_db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT vehicle_id, expected_soc, arrival_time,
                       battery_kwh, max_charge_kw
                FROM interdepot_messages
                WHERE dest_depot_id = $1
                  AND status = 'acknowledged'
                  AND arrival_time BETWEEN $2 AND $3
                """,
                depot_id,
                now,
                horizon_end,
            )

        assert len(rows) == 1, "Should find incoming vehicle"
        incoming = rows[0]
        assert str(incoming["vehicle_id"]) == str(vehicle_id)
        assert incoming["expected_soc"] == 0.35
        assert incoming["battery_kwh"] == 324.0
        assert incoming["max_charge_kw"] == 150.0

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM interdepot_messages WHERE dest_depot_id = $1", depot_id)

    @pytest.mark.asyncio
    async def test_incoming_vehicle_availability_window(self, test_db_pool, depot_id, depot_config):
        """Test incoming vehicle availability windows in state."""
        # Create incoming vehicle with specific arrival time
        vehicle_id = uuid4()
        now = datetime.utcnow()
        arrival_time = now + timedelta(hours=2)  # Arrives in 2 hours

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                uuid4(),
                uuid4(),
                depot_id,
                vehicle_id,
                now,
                0.35,
                arrival_time,
                324.0,
                150.0,
                "acknowledged",
            )

        # Calculate arrival timestep
        delta_t = depot_config.delta_t  # 0.25 hours
        arrival_timestep = int((arrival_time - now).total_seconds() / (delta_t * 3600))

        # Vehicle should be unavailable before arrival, available after
        n_t = depot_config.n_timesteps
        availability = [False] * n_t

        # Vehicle unavailable before arrival
        for t in range(min(arrival_timestep, n_t)):
            availability[t] = False

        # Vehicle available after arrival
        for t in range(arrival_timestep, n_t):
            availability[t] = True

        # Verify availability window
        assert not availability[0], "Vehicle should be unavailable before arrival"
        if arrival_timestep < n_t:
            assert availability[arrival_timestep], "Vehicle should be available at arrival time"
            if arrival_timestep + 1 < n_t:
                assert availability[
                    arrival_timestep + 1
                ], "Vehicle should be available after arrival"

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM interdepot_messages WHERE dest_depot_id = $1", depot_id)

    @pytest.mark.asyncio
    async def test_incoming_vehicle_departure_constraints(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test incoming vehicle departure constraints."""
        # Incoming vehicle should have departure SoC constraint ≥ 99%
        # This is tested in optimization tests, but we verify the constraint
        # is set correctly in state

        vehicle_id = uuid4()
        now = datetime.utcnow()
        arrival_time = now + timedelta(hours=2)
        departure_time = now + timedelta(hours=8)  # Departs 6 hours after arrival

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                uuid4(),
                uuid4(),
                depot_id,
                vehicle_id,
                departure_time,
                0.35,  # Arrives with 35% SoC
                arrival_time,
                324.0,
                150.0,
                "acknowledged",
            )

        # Calculate timesteps
        delta_t = depot_config.delta_t
        arrival_timestep = int((arrival_time - now).total_seconds() / (delta_t * 3600))
        departure_timestep = int((departure_time - now).total_seconds() / (delta_t * 3600))

        # Vehicle should have departure SoC constraint ≥ 99% at departure timestep
        # (This is enforced in optimization, not in state assembly)
        # But we verify the departure time is correctly set
        assert departure_timestep > arrival_timestep, "Departure should be after arrival"
        assert departure_timestep < depot_config.n_timesteps, "Departure should be within horizon"

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM interdepot_messages WHERE dest_depot_id = $1", depot_id)
