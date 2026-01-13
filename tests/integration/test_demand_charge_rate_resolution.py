"""Integration tests for demand charge rate resolution.

Per PRD Section 8.1, demand charge rate resolution priority is:
1. prices.demand_kw (most recent price row)
2. depots.demand_charge_rate_kw
3. Default $20/kW

Reference: PRD_v2.md#8-1-optimization-formulation
"""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg

from src.core.state.assembler import StateAssembler
from src.core.models import DepotConfig


@pytest.mark.integration
@pytest.mark.asyncio
class TestDemandChargeRateResolution:
    """Test demand charge rate resolution in integration scenarios."""

    @pytest_asyncio.fixture
    async def test_db_pool(self):
        """Create test database connection pool."""
        import os

        db_url = os.getenv(
            'TEST_DATABASE_URL',
            'postgresql://postgres:postgres@localhost:5432/favonius_test',
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
                'Test Depot',
                34.0522,
                -118.2437,
                'America/Los_Angeles',
                1000.0,
                25.0,  # Depot config has $25/kW
            )

        yield depot_id

        async with test_db_pool.acquire() as conn:
            await conn.execute('DELETE FROM prices WHERE depot_id = $1', depot_id)
            await conn.execute('DELETE FROM depots WHERE depot_id = $1', depot_id)

    @pytest.fixture
    def depot_config(self):
        """Depot configuration."""
        return DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            vehicle_max_charge_kw={'bus_1': 80.0},
            charger_groups={80.0: 2},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )

    @pytest.mark.asyncio
    async def test_demand_charge_rate_from_prices_affects_optimization(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test demand charge rate from prices.demand_kw affects optimization objective."""
        # Insert price row with demand_kw
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
                'utility_tou',
                30.0,  # prices.demand_kw = $30/kW (should take precedence over depot $25/kW)
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)
        rate = await assembler._get_demand_charge_rate()

        assert rate == 30.0, (
            "Should use prices.demand_kw ($30/kW) over depot config ($25/kW)"
        )

    @pytest.mark.asyncio
    async def test_demand_charge_rate_fallback_to_depot_config(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test demand charge rate falls back to depot config when prices.demand_kw is NULL."""
        # Insert price row WITHOUT demand_kw
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
                'utility_tou',
                None,  # prices.demand_kw is NULL
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)
        rate = await assembler._get_demand_charge_rate()

        assert rate == 25.0, (
            "Should fall back to depot config ($25/kW) when prices.demand_kw is NULL"
        )

    @pytest.mark.asyncio
    async def test_demand_charge_rate_most_recent_price(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test that most recent price row is used when multiple price rows exist."""
        now = datetime.utcnow()
        
        # Insert older price row
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO prices (
                    depot_id, time, price_kwh, source, demand_kw
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                depot_id,
                now - timedelta(hours=2),
                0.10,
                'utility_tou',
                20.0,  # Older price has $20/kW
            )

        # Insert newer price row
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO prices (
                    depot_id, time, price_kwh, source, demand_kw
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                depot_id,
                now,
                0.12,
                'utility_tou',
                35.0,  # Newer price has $35/kW (should be used)
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)
        rate = await assembler._get_demand_charge_rate()

        assert rate == 35.0, (
            "Should use most recent price row ($35/kW), not older row ($20/kW)"
        )

    @pytest.mark.asyncio
    async def test_demand_charge_rate_default_fallback(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test demand charge rate falls back to default when both are NULL."""
        # Create depot with NULL demand_charge_rate_kw
        async with test_db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE depots
                SET demand_charge_rate_kw = NULL
                WHERE depot_id = $1
                """,
                depot_id,
            )

        # No price rows with demand_kw
        assembler = StateAssembler(test_db_pool, depot_id, depot_config)
        rate = await assembler._get_demand_charge_rate()

        assert rate == 20.0, (
            "Should use default $20/kW when both prices.demand_kw and depot config are NULL"
        )

    @pytest.mark.asyncio
    async def test_demand_charge_rate_resolution_in_state_assembly(
        self, test_db_pool, depot_id, depot_config
    ):
        """Test demand charge rate resolution in full state assembly flow."""
        # Insert price row with demand_kw
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
                'utility_tou',
                28.0,  # prices.demand_kw = $28/kW
            )

        assembler = StateAssembler(test_db_pool, depot_id, depot_config)
        
        # State assembly should use demand charge rate from prices
        state = await assembler.get_current_state()

        assert state.demand_charge_rate == 28.0, (
            "State assembly should use demand charge rate from prices.demand_kw"
        )
