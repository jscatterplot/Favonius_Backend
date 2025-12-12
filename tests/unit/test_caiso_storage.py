"""Unit tests for CAISO price storage functionality.

Reference: Development plan Step 3.2, PRD.md#6-1-database-schema
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg

from src.adapters.caiso.storage import (
    store_prices,
    get_cached_prices,
    get_latest_price,
)
from src.adapters.caiso.prices import CAISOPrice


# ============ Fixtures ============

@pytest.fixture
def mock_db_pool():
    """Mock database connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def depot_id():
    """Sample depot ID."""
    return str(uuid4())


@pytest.fixture
def sample_prices():
    """Sample CAISO prices."""
    base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    return [
        CAISOPrice(
            timestamp=base_time + timedelta(hours=i),
            lmp=50.0 + i * 5,  # $/MWh
            node='DLAP_SCE-APND',
        )
        for i in range(24)
    ]


# ============ Store Prices Tests ============

class TestStorePrices:
    """Tests for store_prices function."""

    @pytest.mark.asyncio
    async def test_store_prices_success(
        self, mock_db_pool, depot_id, sample_prices
    ):
        """Test successful price storage."""
        pool, conn = mock_db_pool
        
        count = await store_prices(pool, sample_prices, depot_id)
        
        assert count == len(sample_prices)
        assert conn.execute.call_count == len(sample_prices)

    @pytest.mark.asyncio
    async def test_store_prices_empty_list(self, mock_db_pool, depot_id):
        """Test storage of empty price list."""
        pool, conn = mock_db_pool
        
        count = await store_prices(pool, [], depot_id)
        
        assert count == 0
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_prices_converts_lmp_to_kwh(
        self, mock_db_pool, depot_id
    ):
        """Test that LMP ($/MWh) is converted to $/kWh."""
        pool, conn = mock_db_pool
        
        prices = [
            CAISOPrice(
                timestamp=datetime.utcnow(),
                lmp=100.0,  # $/MWh
                node='NODE',
            )
        ]
        
        await store_prices(pool, prices, depot_id)
        
        # Verify the call was made with converted price
        call_args = conn.execute.call_args
        args = call_args[0]
        # args[3] should be energy_kwh (0th is query, 1 is time, 2 is depot_id, 3 is energy_kwh)
        energy_kwh = args[3]
        assert energy_kwh == 0.1  # 100 $/MWh = 0.1 $/kWh

    @pytest.mark.asyncio
    async def test_store_prices_with_demand_charge(
        self, mock_db_pool, depot_id, sample_prices
    ):
        """Test storage with demand charge rate."""
        pool, conn = mock_db_pool
        
        demand_charge = 20.0  # $/kW
        
        await store_prices(
            pool, sample_prices[:1], depot_id, 
            demand_charge_per_kw=demand_charge
        )
        
        call_args = conn.execute.call_args
        args = call_args[0]
        # args[4] should be demand_kw
        assert args[4] == demand_charge

    @pytest.mark.asyncio
    async def test_store_prices_with_custom_source(
        self, mock_db_pool, depot_id, sample_prices
    ):
        """Test storage with custom price source."""
        pool, conn = mock_db_pool
        
        await store_prices(
            pool, sample_prices[:1], depot_id,
            source='utility_tou'
        )
        
        call_args = conn.execute.call_args
        args = call_args[0]
        # args[5] should be source
        assert args[5] == 'utility_tou'

    @pytest.mark.asyncio
    async def test_store_prices_handles_db_error(
        self, mock_db_pool, depot_id, sample_prices
    ):
        """Test storage handles database errors."""
        pool, conn = mock_db_pool
        conn.execute.side_effect = asyncpg.PostgresError("DB error")
        
        with pytest.raises(asyncpg.PostgresError):
            await store_prices(pool, sample_prices, depot_id)

    @pytest.mark.asyncio
    async def test_store_prices_with_uuid_depot_id(
        self, mock_db_pool, sample_prices
    ):
        """Test storage works with UUID depot_id."""
        pool, conn = mock_db_pool
        depot_uuid = uuid4()
        
        count = await store_prices(pool, sample_prices[:1], depot_uuid)
        
        assert count == 1


# ============ Duplicate Handling Tests ============

class TestDuplicateHandling:
    """Tests for duplicate price handling (upsert behavior)."""

    @pytest.mark.asyncio
    async def test_store_prices_upsert_query(self, mock_db_pool, depot_id):
        """Test that query uses ON CONFLICT for upsert."""
        pool, conn = mock_db_pool
        
        prices = [
            CAISOPrice(
                timestamp=datetime.utcnow(),
                lmp=100.0,
                node='NODE',
            )
        ]
        
        await store_prices(pool, prices, depot_id)
        
        # Check that query contains ON CONFLICT
        call_args = conn.execute.call_args
        query = call_args[0][0]
        assert 'ON CONFLICT' in query
        assert 'DO UPDATE' in query

    @pytest.mark.asyncio
    async def test_store_duplicate_timestamps(
        self, mock_db_pool, depot_id
    ):
        """Test that duplicate timestamps are handled via upsert."""
        pool, conn = mock_db_pool
        
        timestamp = datetime.utcnow()
        prices = [
            CAISOPrice(timestamp=timestamp, lmp=100.0, node='NODE'),
            CAISOPrice(timestamp=timestamp, lmp=150.0, node='NODE'),  # Same timestamp
        ]
        
        count = await store_prices(pool, prices, depot_id)
        
        # Both should be stored (second will upsert first)
        assert count == 2


# ============ Get Cached Prices Tests ============

class TestGetCachedPrices:
    """Tests for get_cached_prices function."""

    @pytest.mark.asyncio
    async def test_get_cached_prices_success(self, mock_db_pool, depot_id):
        """Test successful price retrieval."""
        pool, conn = mock_db_pool
        
        base_time = datetime.utcnow()
        mock_rows = [
            MagicMock(
                __iter__=lambda s: iter([
                    ('time', base_time),
                    ('energy_kwh', 0.10),
                    ('demand_kw', 15.0),
                    ('source', 'caiso_dam'),
                ])
            ),
        ]
        mock_rows[0].__getitem__ = lambda s, k: {
            'time': base_time,
            'energy_kwh': 0.10,
            'demand_kw': 15.0,
            'source': 'caiso_dam',
        }[k]
        conn.fetch.return_value = mock_rows
        
        start = base_time - timedelta(hours=1)
        end = base_time + timedelta(hours=1)
        
        prices = await get_cached_prices(pool, depot_id, start, end)
        
        assert len(prices) == 1
        conn.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_cached_prices_empty(self, mock_db_pool, depot_id):
        """Test retrieval when no prices found."""
        pool, conn = mock_db_pool
        conn.fetch.return_value = []
        
        start = datetime.utcnow() - timedelta(hours=1)
        end = datetime.utcnow()
        
        prices = await get_cached_prices(pool, depot_id, start, end)
        
        assert prices == []

    @pytest.mark.asyncio
    async def test_get_cached_prices_handles_db_error(self, mock_db_pool, depot_id):
        """Test retrieval handles database errors."""
        pool, conn = mock_db_pool
        conn.fetch.side_effect = asyncpg.PostgresError("DB error")
        
        start = datetime.utcnow() - timedelta(hours=1)
        end = datetime.utcnow()
        
        with pytest.raises(asyncpg.PostgresError):
            await get_cached_prices(pool, depot_id, start, end)

    @pytest.mark.asyncio
    async def test_get_cached_prices_time_range(self, mock_db_pool, depot_id):
        """Test that time range is correctly passed to query."""
        pool, conn = mock_db_pool
        conn.fetch.return_value = []
        
        start = datetime(2025, 1, 1, 0, 0, 0)
        end = datetime(2025, 1, 2, 0, 0, 0)
        
        await get_cached_prices(pool, depot_id, start, end)
        
        call_args = conn.fetch.call_args
        args = call_args[0]
        # args[2] should be start_time, args[3] should be end_time
        assert args[1] == str(depot_id)  # depot_id_str
        assert args[2] == start
        assert args[3] == end


# ============ Get Latest Price Tests ============

class TestGetLatestPrice:
    """Tests for get_latest_price function."""

    @pytest.mark.asyncio
    async def test_get_latest_price_success(self, mock_db_pool, depot_id):
        """Test successful latest price retrieval."""
        pool, conn = mock_db_pool
        
        mock_row = {
            'time': datetime.utcnow(),
            'energy_kwh': 0.12,
            'demand_kw': 18.0,
            'source': 'caiso_rtm',
        }
        conn.fetchrow.return_value = mock_row
        
        price = await get_latest_price(pool, depot_id)
        
        assert price is not None
        assert price['energy_kwh'] == 0.12
        assert price['source'] == 'caiso_rtm'

    @pytest.mark.asyncio
    async def test_get_latest_price_none(self, mock_db_pool, depot_id):
        """Test retrieval when no prices exist."""
        pool, conn = mock_db_pool
        conn.fetchrow.return_value = None
        
        price = await get_latest_price(pool, depot_id)
        
        assert price is None

    @pytest.mark.asyncio
    async def test_get_latest_price_handles_db_error(self, mock_db_pool, depot_id):
        """Test retrieval handles database errors."""
        pool, conn = mock_db_pool
        conn.fetchrow.side_effect = asyncpg.PostgresError("DB error")
        
        with pytest.raises(asyncpg.PostgresError):
            await get_latest_price(pool, depot_id)


# ============ Gap Filling Tests ============

class TestGapFillingLogic:
    """Tests for price gap filling and interpolation.
    
    Note: Current implementation does not include gap filling;
    these tests document expected behavior if implemented.
    """

    @pytest.mark.asyncio
    async def test_identify_price_gaps(self, mock_db_pool, depot_id):
        """Test identifying gaps in price data."""
        pool, conn = mock_db_pool
        
        base_time = datetime(2025, 1, 1, 0, 0, 0)
        # Return prices with a gap (missing hour 2)
        mock_rows = [
            {'time': base_time, 'energy_kwh': 0.10, 'demand_kw': 15.0, 'source': 'caiso'},
            {'time': base_time + timedelta(hours=1), 'energy_kwh': 0.11, 'demand_kw': 15.0, 'source': 'caiso'},
            # Hour 2 missing
            {'time': base_time + timedelta(hours=3), 'energy_kwh': 0.13, 'demand_kw': 15.0, 'source': 'caiso'},
        ]
        conn.fetch.return_value = mock_rows
        
        prices = await get_cached_prices(
            pool, depot_id, base_time, base_time + timedelta(hours=4)
        )
        
        # Identify gaps (this would be in application logic, not storage)
        times = [p['time'] for p in prices]
        expected_times = [base_time + timedelta(hours=i) for i in range(4)]
        gaps = [t for t in expected_times if t not in times]
        
        assert len(gaps) == 1
        assert gaps[0] == base_time + timedelta(hours=2)


# ============ Price Source Tests ============

class TestPriceSourceHandling:
    """Tests for different price sources."""

    @pytest.mark.asyncio
    async def test_store_dam_prices(self, mock_db_pool, depot_id, sample_prices):
        """Test storing Day-Ahead Market prices."""
        pool, conn = mock_db_pool
        
        await store_prices(pool, sample_prices[:1], depot_id, source='caiso_dam')
        
        call_args = conn.execute.call_args[0]
        assert call_args[5] == 'caiso_dam'

    @pytest.mark.asyncio
    async def test_store_rtm_prices(self, mock_db_pool, depot_id, sample_prices):
        """Test storing Real-Time Market prices."""
        pool, conn = mock_db_pool
        
        await store_prices(pool, sample_prices[:1], depot_id, source='caiso_rtm')
        
        call_args = conn.execute.call_args[0]
        assert call_args[5] == 'caiso_rtm'

    @pytest.mark.asyncio
    async def test_store_utility_tou_prices(self, mock_db_pool, depot_id, sample_prices):
        """Test storing utility TOU prices."""
        pool, conn = mock_db_pool
        
        await store_prices(pool, sample_prices[:1], depot_id, source='utility_tou')
        
        call_args = conn.execute.call_args[0]
        assert call_args[5] == 'utility_tou'


# ============ Timestamp Handling Tests ============

class TestTimestampHandling:
    """Tests for timestamp handling."""

    @pytest.mark.asyncio
    async def test_store_utc_timestamps(self, mock_db_pool, depot_id):
        """Test that UTC timestamps are stored correctly."""
        pool, conn = mock_db_pool
        
        utc_time = datetime(2025, 1, 15, 12, 0, 0)  # Noon UTC
        prices = [CAISOPrice(timestamp=utc_time, lmp=100.0, node='NODE')]
        
        await store_prices(pool, prices, depot_id)
        
        call_args = conn.execute.call_args[0]
        stored_time = call_args[1]
        assert stored_time == utc_time

    @pytest.mark.asyncio
    async def test_retrieve_ordered_by_time(self, mock_db_pool, depot_id):
        """Test that retrieved prices are ordered by time."""
        pool, conn = mock_db_pool
        
        # Check that query includes ORDER BY
        conn.fetch.return_value = []
        
        await get_cached_prices(
            pool, depot_id,
            datetime(2025, 1, 1), datetime(2025, 1, 2)
        )
        
        query = conn.fetch.call_args[0][0]
        assert 'ORDER BY time' in query


# ============ Edge Cases ============

class TestEdgeCases:
    """Edge case tests for CAISO storage."""

    @pytest.mark.asyncio
    async def test_store_negative_lmp(self, mock_db_pool, depot_id):
        """Test storing negative LMP (can happen during oversupply)."""
        pool, conn = mock_db_pool
        
        prices = [
            CAISOPrice(
                timestamp=datetime.utcnow(),
                lmp=-10.0,  # Negative LMP
                node='NODE',
            )
        ]
        
        await store_prices(pool, prices, depot_id)
        
        call_args = conn.execute.call_args[0]
        energy_kwh = call_args[3]
        assert energy_kwh == -0.01  # -10 $/MWh = -0.01 $/kWh

    @pytest.mark.asyncio
    async def test_store_very_high_lmp(self, mock_db_pool, depot_id):
        """Test storing very high LMP (price spike)."""
        pool, conn = mock_db_pool
        
        prices = [
            CAISOPrice(
                timestamp=datetime.utcnow(),
                lmp=1000.0,  # $1000/MWh price spike
                node='NODE',
            )
        ]
        
        await store_prices(pool, prices, depot_id)
        
        call_args = conn.execute.call_args[0]
        energy_kwh = call_args[3]
        assert energy_kwh == 1.0  # 1000 $/MWh = 1.0 $/kWh

    @pytest.mark.asyncio
    async def test_store_zero_lmp(self, mock_db_pool, depot_id):
        """Test storing zero LMP."""
        pool, conn = mock_db_pool
        
        prices = [
            CAISOPrice(
                timestamp=datetime.utcnow(),
                lmp=0.0,
                node='NODE',
            )
        ]
        
        await store_prices(pool, prices, depot_id)
        
        call_args = conn.execute.call_args[0]
        energy_kwh = call_args[3]
        assert energy_kwh == 0.0

