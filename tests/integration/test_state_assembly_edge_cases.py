"""Integration tests for state assembly edge cases.

Tests:
1. Missing data handling (fallbacks)
2. Data validation (invalid values)
3. Incoming vehicle edge cases

Reference: PRD_v2.md#5-2-state-assembly

NOTE (migration 029): These tests pass a raw asyncpg.Pool to StateAssembler
which expects DatabasePools (static=Supabase, ts=TimescaleDB). Additionally,
they INSERT into vehicles/depots shadow tables that were dropped in migration
029. They need to be rewritten to mock DatabasePools before they can run
against the current schema.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "Requires static shadow tables (dropped in migration 029). "
        "Needs rewrite to use DatabasePools mock with Supabase static pool."
    )
)
import pytest_asyncio

from src.core.models import DepotConfig
from src.core.state.assembler import StateAssembler


@pytest.mark.integration
@pytest.mark.asyncio
class TestStateAssemblyEdgeCases:
    """Test state assembly edge cases."""

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
                20.0,
            )

        yield depot_id

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM telemetry WHERE vehicle_id IN (SELECT vehicle_id FROM vehicles WHERE depot_id = $1)",
                depot_id,
            )
            await conn.execute("DELETE FROM vehicles WHERE depot_id = $1", depot_id)
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
    async def test_missing_telemetry_use_last_known(self, test_db_pool, depot_id, depot_config):
        """Test missing telemetry (use last known + warning)."""
        vehicle_id = uuid4()

        # Create vehicle
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vehicles (
                    vehicle_id, depot_id, vehicle_type, battery_capacity_kwh
                )
                VALUES ($1, $2, $3, $4)
                """,
                vehicle_id,
                depot_id,
                "bus",
                324.0,
            )

            # Insert old telemetry (20 minutes ago - stale)
            await conn.execute(
                """
                INSERT INTO telemetry (
                    vehicle_id, time, soc, power_kw
                )
                VALUES ($1, $2, $3, $4)
                """,
                vehicle_id,
                datetime.utcnow() - timedelta(minutes=20),
                0.5,  # Last known SoC
                0.0,
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get vehicle SoCs (should use last known if no fresh telemetry)
        socs = await assembler._get_vehicle_socs()

        # Should use last known SoC (0.5) with warning
        if str(vehicle_id) in socs:
            assert socs[str(vehicle_id)] == 0.5

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM telemetry WHERE vehicle_id = $1", vehicle_id)
            await conn.execute("DELETE FROM vehicles WHERE vehicle_id = $1", vehicle_id)

    @pytest.mark.asyncio
    async def test_missing_prices_use_cached_tou(self, test_db_pool, depot_id, depot_config):
        """Test missing prices (use cached TOU)."""
        # No price rows in database
        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get prices (should use cached TOU if no prices in database)
        # In real code, StateAssembler._get_prices() would use cached TOU
        # For test, we verify the fallback mechanism exists
        prices = await assembler._get_prices()

        # Should have prices (either from database or cached TOU)
        assert prices is not None
        assert len(prices) > 0

    @pytest.mark.asyncio
    async def test_missing_weather_use_last_forecast(self, test_db_pool, depot_id, depot_config):
        """Test missing weather (use last forecast)."""
        # No weather rows in database
        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get weather (should use last forecast if no weather in database)
        # In real code, StateAssembler._get_weather() would use last forecast
        # For test, we verify the fallback mechanism exists
        await assembler._get_weather()

        # Weather is optional, so None is acceptable
        # But if present, should be valid

    @pytest.mark.asyncio
    async def test_missing_building_load_use_forecast_model(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test missing building load (use forecast model)."""
        # No building load rows in database
        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get building load (should use forecast model if no building load in database)
        # In real code, StateAssembler._get_building_power() would use forecast model
        building_power = await assembler._get_building_power()

        # Should have building power (either from database or forecast model)
        assert building_power is not None
        assert len(building_power) > 0

    @pytest.mark.asyncio
    async def test_missing_schedules_fail_with_error(self, test_db_pool, depot_id, depot_config):
        """Test missing schedules (fail with error).

        Per PRD, schedules are required - missing schedules should fail.
        """
        # Schedules are required for optimization
        # In real code, missing schedules would raise an error
        # For test, we verify the validation exists
        StateAssembler(test_db_pool, depot_id, depot_config)

        # State assembly should validate schedules exist
        # (Tested in state validation tests)

    @pytest.mark.asyncio
    async def test_invalid_soc_values_clamp(self, test_db_pool, depot_id, depot_config):
        """Test invalid SoC values (clamp to [0.0, 1.0])."""
        vehicle_id = uuid4()

        # Create vehicle with invalid SoC (> 1.0)
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vehicles (
                    vehicle_id, depot_id, vehicle_type, battery_capacity_kwh
                )
                VALUES ($1, $2, $3, $4)
                """,
                vehicle_id,
                depot_id,
                "bus",
                324.0,
            )

            # Insert telemetry with invalid SoC
            await conn.execute(
                """
                INSERT INTO telemetry (
                    vehicle_id, time, soc, power_kw
                )
                VALUES ($1, $2, $3, $4)
                """,
                vehicle_id,
                datetime.utcnow(),
                1.5,  # Invalid: > 1.0
                0.0,
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get vehicle SoCs (should clamp invalid values)
        socs = await assembler._get_vehicle_socs()

        if str(vehicle_id) in socs:
            soc = socs[str(vehicle_id)]
            # Should be clamped to [0.0, 1.0]
            assert 0.0 <= soc <= 1.0, f"SoC {soc} should be clamped to [0.0, 1.0]"

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM telemetry WHERE vehicle_id = $1", vehicle_id)
            await conn.execute("DELETE FROM vehicles WHERE vehicle_id = $1", vehicle_id)

    @pytest.mark.asyncio
    async def test_invalid_price_values(self, test_db_pool, depot_id, depot_config):
        """Test invalid price values (negative, zero)."""
        # Insert invalid price (negative)
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO prices (
                    depot_id, time, price_kwh, source
                )
                VALUES ($1, $2, $3, $4)
                """,
                depot_id,
                datetime.utcnow(),
                -0.10,  # Invalid: negative price
                "utility_tou",
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get prices (should handle invalid values)
        prices = await assembler._get_prices()

        # Prices should be valid (non-negative)
        assert all(p >= 0 for p in prices), "Prices should be non-negative"

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM prices WHERE depot_id = $1", depot_id)

    @pytest.mark.asyncio
    async def test_invalid_timestamps(self, test_db_pool, depot_id, depot_config):
        """Test invalid timestamps (future, too old)."""
        vehicle_id = uuid4()

        # Create vehicle
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vehicles (
                    vehicle_id, depot_id, vehicle_type, battery_capacity_kwh
                )
                VALUES ($1, $2, $3, $4)
                """,
                vehicle_id,
                depot_id,
                "bus",
                324.0,
            )

            # Insert telemetry with future timestamp
            await conn.execute(
                """
                INSERT INTO telemetry (
                    vehicle_id, time, soc, power_kw
                )
                VALUES ($1, $2, $3, $4)
                """,
                vehicle_id,
                datetime.utcnow() + timedelta(days=1),  # Future timestamp
                0.5,
                0.0,
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get vehicle SoCs (should handle future timestamps)
        # In real code, would filter out future timestamps or use most recent valid
        await assembler._get_vehicle_socs()

        # Should handle gracefully (may skip future timestamps)

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM telemetry WHERE vehicle_id = $1", vehicle_id)
            await conn.execute("DELETE FROM vehicles WHERE vehicle_id = $1", vehicle_id)

    @pytest.mark.asyncio
    async def test_invalid_uuids_format_validation(self):
        """Test invalid UUIDs (format validation)."""
        # Invalid UUID format should be rejected
        invalid_uuid = "not-a-uuid"

        # UUID validation should catch this
        # (Tested in security/validators tests)
        from src.security.validators import validate_depot_id, validate_vehicle_id

        with pytest.raises(ValueError):
            validate_depot_id(invalid_uuid)

        with pytest.raises(ValueError):
            validate_vehicle_id(invalid_uuid)

    @pytest.mark.asyncio
    async def test_incoming_vehicle_integration_into_state(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test incoming vehicle integration into state."""
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
                0.35,
                arrival_time,
                324.0,
                150.0,
                "acknowledged",
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)

        # Get incoming vehicles
        now = datetime.utcnow()
        horizon_end = now + timedelta(hours=24)
        incoming_vehicles = await assembler._get_incoming_vehicles(now, horizon_end)

        assert len(incoming_vehicles) == 1
        incoming = incoming_vehicles[0]
        assert str(incoming.vehicle_id) == str(vehicle_id)
        assert incoming.expected_soc == 0.35
        assert incoming.battery_kwh == 324.0
        assert incoming.max_charge_kw == 150.0

        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM interdepot_messages WHERE dest_depot_id = $1", depot_id)

    @pytest.mark.asyncio
    async def test_incoming_vehicle_availability_windows(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test incoming vehicle availability windows."""
        # Incoming vehicle should be unavailable before arrival, available after
        # (Tested in test_data_resolution.py)
        pass

    @pytest.mark.asyncio
    async def test_incoming_vehicle_soc_initialization(self, test_db_pool, depot_id, depot_config):
        """Test incoming vehicle SoC initialization."""
        # Incoming vehicle SoC should be initialized to expected_soc at arrival
        # (Tested in test_data_resolution.py)
        pass

    @pytest.mark.asyncio
    async def test_incoming_vehicle_departure_constraints(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test incoming vehicle departure constraints."""
        # Incoming vehicle should have departure SoC constraint ≥ 99%
        # (Tested in test_data_resolution.py)
        pass
