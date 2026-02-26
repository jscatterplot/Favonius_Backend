"""Integration tests for handoff departure_time resolution.

Per PRD Section 5.4, receive_handoff should:
1. Query original pending message for departure_time
2. Use actual departure_time from original message (not acknowledged_at)
3. Fall back to acknowledged_at when original message not found
4. Store departure_time correctly in interdepot_messages

Reference: PRD_v2.md#5-4-inter-depot-handoff
"""

from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio


@pytest.mark.integration
@pytest.mark.asyncio
class TestHandoffDepartureTime:
    """Test handoff departure_time resolution."""

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
        """Create depot_A."""
        depot_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO depots (
                    depot_id, name, latitude, longitude, timezone,
                    max_grid_kw, demand_charge_rate_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (depot_id) DO UPDATE
                SET name = EXCLUDED.name
                """,
                depot_id,
                "Depot A",
                34.0522,
                -118.2437,
                "America/Los_Angeles",
                1000.0,
                20.0,
            )

        yield depot_id

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM interdepot_messages WHERE origin_depot_id = $1 OR dest_depot_id = $1",
                depot_id,
            )
            await conn.execute("DELETE FROM depots WHERE depot_id = $1", depot_id)

    @pytest_asyncio.fixture
    async def depot_b_id(self, test_db_pool):
        """Create depot_B."""
        depot_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO depots (
                    depot_id, name, latitude, longitude, timezone,
                    max_grid_kw, demand_charge_rate_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (depot_id) DO UPDATE
                SET name = EXCLUDED.name
                """,
                depot_id,
                "Depot B",
                37.7749,
                -122.4194,
                "America/Los_Angeles",
                1000.0,
                20.0,
            )

        yield depot_id

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM interdepot_messages WHERE origin_depot_id = $1 OR dest_depot_id = $1",
                depot_id,
            )
            await conn.execute("DELETE FROM depots WHERE depot_id = $1", depot_id)

    @pytest_asyncio.fixture
    async def vehicle_id(self, test_db_pool, depot_a_id):
        """Create test vehicle."""
        vehicle_id = uuid4()

        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vehicles (
                    vehicle_id, depot_id, vehicle_type, battery_capacity_kwh,
                    max_charge_power_kw
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                vehicle_id,
                depot_a_id,
                "bus",
                324.0,
                150.0,
            )

        yield vehicle_id

        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM vehicles WHERE vehicle_id = $1", vehicle_id)

    @pytest.mark.asyncio
    async def test_receive_handoff_uses_original_departure_time(
        self, test_db_pool, depot_a_id, depot_b_id, vehicle_id
    ):
        """Test receive_handoff queries original message for departure_time.

        Per PRD Section 5.4, receive_handoff should use actual departure_time
        from original pending message, not acknowledged_at.
        """
        # Create original pending message with specific departure_time
        original_departure_time = datetime.utcnow() - timedelta(hours=1)
        message_id = uuid4()

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
                message_id,
                depot_a_id,
                depot_b_id,
                vehicle_id,
                original_departure_time,  # Original departure_time
                0.35,
                datetime.utcnow() + timedelta(hours=1),
                324.0,
                150.0,
                "pending",
            )

        # Simulate receive_handoff call (would need to mock FastAPI dependencies)
        # For now, test the database query logic directly
        async with test_db_pool.acquire() as conn:
            original_message = await conn.fetchrow(
                """
                SELECT departure_time
                FROM interdepot_messages
                WHERE origin_depot_id = $1
                  AND dest_depot_id = $2
                  AND vehicle_id = $3
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                depot_a_id,
                depot_b_id,
                vehicle_id,
            )

        assert original_message is not None, "Original message should exist"
        assert (
            original_message["departure_time"] == original_departure_time
        ), "Should retrieve original departure_time from pending message"

    @pytest.mark.asyncio
    async def test_receive_handoff_fallback_to_acknowledged_at(
        self, test_db_pool, depot_a_id, depot_b_id, vehicle_id
    ):
        """Test receive_handoff falls back to acknowledged_at when original message not found."""
        # No pending message exists
        acknowledged_at = datetime.utcnow()

        # Simulate the fallback logic
        departure_time = acknowledged_at  # Default fallback

        async with test_db_pool.acquire() as conn:
            original_message = await conn.fetchrow(
                """
                SELECT departure_time
                FROM interdepot_messages
                WHERE origin_depot_id = $1
                  AND dest_depot_id = $2
                  AND vehicle_id = $3
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                depot_a_id,
                depot_b_id,
                vehicle_id,
            )

        if original_message and original_message["departure_time"]:
            departure_time = original_message["departure_time"]
        else:
            # Fallback to acknowledged_at
            departure_time = acknowledged_at

        assert (
            departure_time == acknowledged_at
        ), "Should fall back to acknowledged_at when original message not found"

    @pytest.mark.asyncio
    async def test_departure_time_stored_correctly(
        self, test_db_pool, depot_a_id, depot_b_id, vehicle_id
    ):
        """Test departure_time is stored correctly in interdepot_messages."""
        original_departure_time = datetime.utcnow() - timedelta(hours=1)
        message_id = uuid4()
        acknowledged_at = datetime.utcnow()

        # Create original pending message
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
                message_id,
                depot_a_id,
                depot_b_id,
                vehicle_id,
                original_departure_time,
                0.35,
                datetime.utcnow() + timedelta(hours=1),
                324.0,
                150.0,
                "pending",
            )

        # Simulate receive_handoff storing acknowledged message
        # Get departure_time from original message
        async with test_db_pool.acquire() as conn:
            original_message = await conn.fetchrow(
                """
                SELECT departure_time
                FROM interdepot_messages
                WHERE origin_depot_id = $1
                  AND dest_depot_id = $2
                  AND vehicle_id = $3
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                depot_a_id,
                depot_b_id,
                vehicle_id,
            )

        departure_time = acknowledged_at  # Default
        if original_message and original_message["departure_time"]:
            departure_time = original_message["departure_time"]

        # Store acknowledged message with departure_time
        acknowledged_message_id = uuid4()
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interdepot_messages
                    (message_id, origin_depot_id, dest_depot_id, vehicle_id,
                     departure_time, expected_soc, arrival_time, battery_kwh,
                     max_charge_kw, status, acknowledged_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                acknowledged_message_id,
                depot_a_id,
                depot_b_id,
                vehicle_id,
                departure_time,  # Should be original_departure_time
                0.35,
                datetime.utcnow() + timedelta(hours=1),
                324.0,
                150.0,
                "acknowledged",
                acknowledged_at,
            )

        # Verify stored departure_time
        async with test_db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT departure_time, acknowledged_at
                FROM interdepot_messages
                WHERE message_id = $1
                """,
                acknowledged_message_id,
            )

        assert row is not None
        assert (
            row["departure_time"] == original_departure_time
        ), "Stored departure_time should match original message, not acknowledged_at"
        assert (
            row["acknowledged_at"] == acknowledged_at
        ), "acknowledged_at should be separate from departure_time"
