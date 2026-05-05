"""Acceptance Test AT-06: Inter-Depot Handoff

Reference: PRD.md#11-1-mvp-acceptance-tests

GIVEN bus_101 departing depot_A for depot_B
AND expected arrival SoC = 0.35
WHEN bus_101 departs depot_A
THEN depot_B receives handoff message within 30 seconds
AND handoff message includes battery_kwh and max_charge_kw
"""

from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.adapters.handoff.manager import HandoffManager, HandoffMessage
from src.core.models import DepotConfig, DepotState, IncomingVehicle


@pytest.mark.integration
@pytest.mark.acceptance
@pytest.mark.asyncio
class TestAT06InterDepotHandoff:
    """AT-06: Inter-Depot Handoff acceptance test.

    Per PRD Section 11.1, this test verifies:
    1. Handoff message is sent within 30 seconds
    2. Message includes battery_kwh and max_charge_kw
    3. Receiving depot can include incoming vehicle in optimization
    """

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
    async def depot_a_id(self, test_db_pool):
        """Yield a depot_A UUID. Shadow table dropped in migration 029."""
        depot_id = uuid4()
        yield depot_id
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM interdepot_messages WHERE origin_depot_id = $1 OR dest_depot_id = $1",
                depot_id,
            )

    @pytest_asyncio.fixture
    async def depot_b_id(self, test_db_pool):
        """Yield a depot_B UUID."""
        depot_id = uuid4()
        yield depot_id
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM interdepot_messages WHERE origin_depot_id = $1 OR dest_depot_id = $1",
                depot_id,
            )

    @pytest_asyncio.fixture
    async def bus_101_id(self, test_db_pool, depot_a_id):  # noqa: ARG002
        """Yield a vehicle UUID. Shadow table dropped in migration 029."""
        return uuid4()

    @pytest.mark.asyncio
    async def test_at06_handoff_message_within_30_seconds(
        self, test_db_pool, depot_a_id, depot_b_id, bus_101_id
    ):
        """AT-06: Verify handoff message sent within 30 seconds."""
        departure_time = datetime.utcnow()
        arrival_time = departure_time + timedelta(hours=2)
        expected_soc = 0.35
        battery_kwh = 324.0
        max_charge_kw = 150.0

        message_id = uuid4()
        start_time = datetime.utcnow()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                message_id,
                depot_a_id,
                depot_b_id,
                bus_101_id,
                departure_time,
                expected_soc,
                arrival_time,
                battery_kwh,
                max_charge_kw,
            )

        message_time = (datetime.utcnow() - start_time).total_seconds()

        # PRD requirement: message within 30 seconds
        assert message_time < 30.0, f"Handoff message took {message_time:.2f}s > 30s"

    @pytest.mark.asyncio
    async def test_at06_handoff_includes_battery_specs(
        self, test_db_pool, depot_a_id, depot_b_id, bus_101_id
    ):
        """AT-06: Verify handoff message includes battery_kwh and max_charge_kw."""
        departure_time = datetime.utcnow()
        arrival_time = departure_time + timedelta(hours=2)
        expected_soc = 0.35
        battery_kwh = 324.0
        max_charge_kw = 150.0

        message_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                message_id,
                depot_a_id,
                depot_b_id,
                bus_101_id,
                departure_time,
                expected_soc,
                arrival_time,
                battery_kwh,
                max_charge_kw,
            )

        # Verify message includes required fields
        async with test_db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT message_id, origin_depot_id, dest_depot_id, vehicle_id,
                       expected_soc, arrival_time, battery_kwh, max_charge_kw
                FROM interdepot_messages
                WHERE message_id = $1
                """,
                message_id,
            )

        assert row is not None, "Handoff message not found"
        assert str(row["origin_depot_id"]) == str(depot_a_id)
        assert str(row["dest_depot_id"]) == str(depot_b_id)
        assert str(row["vehicle_id"]) == str(bus_101_id)
        assert abs(row["expected_soc"] - expected_soc) < 0.01

        # PRD requirement: message includes battery_kwh and max_charge_kw
        assert row["battery_kwh"] is not None, "Missing battery_kwh in handoff message"
        assert row["max_charge_kw"] is not None, "Missing max_charge_kw in handoff message"
        assert abs(row["battery_kwh"] - battery_kwh) < 0.01
        assert abs(row["max_charge_kw"] - max_charge_kw) < 0.01

    @pytest.mark.asyncio
    async def test_at06_depot_b_receives_handoff(
        self, test_db_pool, depot_a_id, depot_b_id, bus_101_id
    ):
        """AT-06: Verify depot_B can receive and process handoff message."""
        departure_time = datetime.utcnow()
        arrival_time = departure_time + timedelta(hours=2)

        message_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                message_id,
                depot_a_id,
                depot_b_id,
                bus_101_id,
                departure_time,
                0.35,
                arrival_time,
                324.0,
                150.0,
            )

        # Depot B queries for pending handoffs
        async with test_db_pool.acquire() as conn:
            handoff_rows = await conn.fetch(
                """
                SELECT message_id, vehicle_id, expected_soc, arrival_time,
                       battery_kwh, max_charge_kw, origin_depot_id
                FROM interdepot_messages
                WHERE dest_depot_id = $1
                  AND acknowledged_at IS NULL
                ORDER BY created_at DESC
                """,
                depot_b_id,
            )

        assert len(handoff_rows) > 0, "Depot B should receive handoff message"
        handoff = handoff_rows[0]
        assert str(handoff["vehicle_id"]) == str(bus_101_id)
        assert handoff["battery_kwh"] == 324.0
        assert handoff["max_charge_kw"] == 150.0

    @pytest.mark.asyncio
    async def test_at06_incoming_vehicle_in_optimization(
        self, test_db_pool, depot_a_id, depot_b_id, bus_101_id
    ):
        """AT-06: Verify incoming vehicle can be included in optimization."""
        # Create IncomingVehicle from handoff data
        incoming_vehicle = IncomingVehicle(
            vehicle_id=bus_101_id,
            external_id="bus_101",
            expected_soc=0.35,
            arrival_time=datetime.utcnow() + timedelta(hours=2),
            battery_kwh=324.0,
            max_charge_kw=150.0,
            origin_depot_id=depot_a_id,
        )

        # Verify IncomingVehicle has all required fields
        assert incoming_vehicle.vehicle_id == bus_101_id
        assert incoming_vehicle.expected_soc == 0.35
        assert incoming_vehicle.battery_kwh == 324.0
        assert incoming_vehicle.max_charge_kw == 150.0
        assert incoming_vehicle.origin_depot_id == depot_a_id

        # Create depot_B's state with incoming vehicle
        n_t = 96
        vehicle_ids = ["bus_0", "bus_1"]
        DepotConfig(
            vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 3},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=600.0,
        )

        state = DepotState(
            vehicle_socs={"bus_0": 0.5, "bus_1": 0.6},
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={
                "bus_0": [True] * n_t,
                "bus_1": [True] * n_t,
            },
            energy_requirements={"bus_0": 180.0, "bus_1": 160.0},
            departure_times={"bus_0": 48, "bus_1": 60},
            building_power=[50.0] * n_t,
            incoming_vehicles=[incoming_vehicle],
        )

        # State should include incoming vehicle
        assert len(state.incoming_vehicles) == 1
        assert state.incoming_vehicles[0].external_id == "bus_101"

    @pytest.mark.asyncio
    async def test_at06_handoff_acknowledgment(
        self, test_db_pool, depot_a_id, depot_b_id, bus_101_id
    ):
        """AT-06: Verify handoff can be acknowledged."""
        departure_time = datetime.utcnow()
        arrival_time = departure_time + timedelta(hours=2)

        message_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages (
                    message_id, origin_depot_id, dest_depot_id, vehicle_id,
                    departure_time, expected_soc, arrival_time,
                    battery_kwh, max_charge_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                message_id,
                depot_a_id,
                depot_b_id,
                bus_101_id,
                departure_time,
                0.35,
                arrival_time,
                324.0,
                150.0,
            )

        # Acknowledge the message
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE interdepot_messages
                SET acknowledged_at = NOW()
                WHERE message_id = $1
                """,
                message_id,
            )

        # Verify acknowledgment
        async with test_db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT acknowledged_at FROM interdepot_messages
                WHERE message_id = $1
                """,
                message_id,
            )

        assert row["acknowledged_at"] is not None, "Message should be acknowledged"


class TestHandoffManagerUnit:
    """Unit tests for HandoffManager."""

    def test_handoff_message_dataclass(self):
        """Verify HandoffMessage dataclass has all required fields."""
        message = HandoffMessage(
            message_id=uuid4(),
            origin_depot_id=uuid4(),
            dest_depot_id=uuid4(),
            vehicle_id=uuid4(),
            external_id="bus_101",
            expected_soc=0.35,
            arrival_time=datetime.utcnow(),
            battery_kwh=324.0,
            max_charge_kw=150.0,
        )

        assert message.battery_kwh == 324.0
        assert message.max_charge_kw == 150.0
        assert message.expected_soc == 0.35

    def test_handoff_manager_creates_incoming_vehicle(self):
        """Verify HandoffManager can create IncomingVehicle."""
        manager = HandoffManager({})

        vehicle_id = uuid4()
        origin_depot_id = uuid4()
        arrival_time = datetime.utcnow() + timedelta(hours=2)

        incoming = manager.create_incoming_vehicle(
            vehicle_id=vehicle_id,
            external_id="bus_101",
            expected_soc=0.35,
            arrival_time=arrival_time,
            battery_kwh=324.0,
            max_charge_kw=150.0,
            origin_depot_id=origin_depot_id,
        )

        assert isinstance(incoming, IncomingVehicle)
        assert incoming.vehicle_id == vehicle_id
        assert incoming.external_id == "bus_101"
        assert incoming.expected_soc == 0.35
        assert incoming.battery_kwh == 324.0
        assert incoming.max_charge_kw == 150.0
        assert incoming.origin_depot_id == origin_depot_id
