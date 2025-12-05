"""Unit tests for CAISO price adapter.

Reference: Development plan Step 3.2, PRD.md#11-2-unit-test-requirements
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.adapters.caiso import (
    CAISOAdapter,
    CAISOPrice,
    get_cached_prices,
    get_latest_price,
    store_prices,
)


# ============ Fixtures ============


@pytest.fixture
def mock_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock(spec=asyncpg.Connection)
    pool.acquire = AsyncMock(return_value=conn)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    return pool


@pytest.fixture
def sample_caiso_prices():
    """Sample CAISO price data."""
    base_time = datetime(2025, 12, 4, 0, 0, 0)
    prices = []
    for hour in range(24):
        # Peak: 4pm-9pm
        if hour in range(16, 21):
            lmp = 250.0
        # Partial-peak: 9am-4pm, 9pm-midnight
        elif hour in range(9, 16) or hour in range(21, 24):
            lmp = 150.0
        # Off-peak: midnight-9am
        else:
            lmp = 80.0

        prices.append(
            CAISOPrice(
                timestamp=base_time + timedelta(hours=hour),
                lmp=lmp,
                energy=lmp * 0.8,
                congestion=lmp * 0.15,
                loss=lmp * 0.05,
                node="SLAP_PGAE-APND",
            )
        )
    return prices


@pytest.fixture
def caiso_adapter(mock_pool):
    """CAISO adapter instance with mocked pool."""
    return CAISOAdapter(pool=mock_pool)


# ============ CAISOPrice Tests ============


def test_caiso_price_creation():
    """Test CAISOPrice dataclass creation."""
    price = CAISOPrice(
        timestamp=datetime(2025, 12, 4, 12, 0, 0),
        lmp=150.0,
        energy=120.0,
        congestion=22.5,
        loss=7.5,
        node="SLAP_PGAE-APND",
    )
    assert price.lmp == 150.0
    assert price.node == "SLAP_PGAE-APND"


# ============ CAISOAdapter Tests ============


@pytest.mark.asyncio
async def test_get_day_ahead_prices_peak_hours(caiso_adapter):
    """Test price generation for peak hours (4pm-9pm)."""
    start = datetime(2025, 12, 4, 16, 0, 0)  # 4 PM
    end = datetime(2025, 12, 4, 21, 0, 0)  # 9 PM

    prices = await caiso_adapter.get_day_ahead_prices(start, end)

    assert len(prices) == 5  # 5 hours
    assert all(p.lmp == 250.0 for p in prices)  # Peak rate


@pytest.mark.asyncio
async def test_get_day_ahead_prices_off_peak_hours(caiso_adapter):
    """Test price generation for off-peak hours (midnight-9am)."""
    start = datetime(2025, 12, 4, 0, 0, 0)  # Midnight
    end = datetime(2025, 12, 4, 9, 0, 0)  # 9 AM

    prices = await caiso_adapter.get_day_ahead_prices(start, end)

    assert len(prices) == 9  # 9 hours
    assert all(p.lmp == 80.0 for p in prices)  # Off-peak rate


@pytest.mark.asyncio
async def test_get_day_ahead_prices_partial_peak_hours(caiso_adapter):
    """Test price generation for partial-peak hours."""
    start = datetime(2025, 12, 4, 9, 0, 0)  # 9 AM
    end = datetime(2025, 12, 4, 16, 0, 0)  # 4 PM

    prices = await caiso_adapter.get_day_ahead_prices(start, end)

    assert len(prices) == 7  # 7 hours
    assert all(p.lmp == 150.0 for p in prices)  # Partial-peak rate


@pytest.mark.asyncio
async def test_get_day_ahead_prices_custom_node(caiso_adapter):
    """Test price generation with custom node."""
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 4, 1, 0, 0)
    custom_node = "CUSTOM_NODE"

    prices = await caiso_adapter.get_day_ahead_prices(start, end, node=custom_node)

    assert len(prices) == 1
    assert prices[0].node == custom_node


@pytest.mark.asyncio
async def test_get_current_price(caiso_adapter):
    """Test getting current price."""
    with patch('src.adapters.caiso.prices.datetime') as mock_dt:
        mock_dt.utcnow.return_value = datetime(2025, 12, 4, 12, 0, 0)
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        price = await caiso_adapter.get_current_price()

        assert price is not None
        assert price.lmp == 150.0  # Partial-peak at noon


@pytest.mark.asyncio
async def test_store_prices_to_db(caiso_adapter, sample_caiso_prices, mock_pool):
    """Test storing prices to database."""
    depot_id = uuid4()
    stored_count = await caiso_adapter.store_prices_to_db(
        sample_caiso_prices[:5], depot_id, source='caiso_dam'
    )

    assert stored_count == 5
    assert mock_pool.acquire.return_value.execute.call_count == 5


@pytest.mark.asyncio
async def test_store_prices_to_db_no_pool(caiso_adapter, sample_caiso_prices):
    """Test storing prices without pool raises error."""
    adapter = CAISOAdapter()  # No pool
    depot_id = uuid4()

    with pytest.raises(RuntimeError, match="Database pool not configured"):
        await adapter.store_prices_to_db(sample_caiso_prices[:5], depot_id)


@pytest.mark.asyncio
async def test_get_prices_for_depot_with_cache(
    caiso_adapter, sample_caiso_prices, mock_pool
):
    """Test getting prices with cache hit."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 4, 24, 0, 0)

    # Mock cached prices
    cached_rows = [
        {
            'time': p.timestamp,
            'energy_kwh': p.lmp / 1000.0,
            'demand_kw': None,
            'source': 'caiso_dam',
        }
        for p in sample_caiso_prices[:5]
    ]
    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=cached_rows)

    prices = await caiso_adapter.get_prices_for_depot(
        depot_id, start, end, use_cache=True
    )

    assert len(prices) == 5
    # Verify prices were converted from cache
    assert all(p.lmp > 0 for p in prices)


@pytest.mark.asyncio
async def test_get_prices_for_depot_no_cache(caiso_adapter, mock_pool):
    """Test getting prices without cache (fresh fetch)."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 4, 24, 0, 0)

    # Mock no cached prices
    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=[])

    prices = await caiso_adapter.get_prices_for_depot(
        depot_id, start, end, use_cache=False
    )

    assert len(prices) == 24  # Full day
    # Verify prices were stored
    assert mock_pool.acquire.return_value.execute.call_count > 0


# ============ Storage Function Tests ============


@pytest.mark.asyncio
async def test_store_prices(mock_pool, sample_caiso_prices):
    """Test store_prices function."""
    depot_id = uuid4()
    stored_count = await store_prices(
        mock_pool, sample_caiso_prices[:10], depot_id, source='caiso_dam'
    )

    assert stored_count == 10
    # Verify execute was called for each price
    assert mock_pool.acquire.return_value.execute.call_count == 10


@pytest.mark.asyncio
async def test_store_prices_empty_list(mock_pool):
    """Test store_prices with empty list."""
    stored_count = await store_prices(mock_pool, [], uuid4())
    assert stored_count == 0


@pytest.mark.asyncio
async def test_store_prices_with_demand_charge(mock_pool, sample_caiso_prices):
    """Test store_prices with demand charge rate."""
    depot_id = uuid4()
    demand_charge = 20.0

    stored_count = await store_prices(
        mock_pool,
        sample_caiso_prices[:5],
        depot_id,
        source='utility_tou',
        demand_charge_per_kw=demand_charge,
    )

    assert stored_count == 5
    # Verify demand_charge was passed to execute
    calls = mock_pool.acquire.return_value.execute.call_args_list
    assert all(call[0][4] == demand_charge for call in calls)


@pytest.mark.asyncio
async def test_get_cached_prices(mock_pool):
    """Test get_cached_prices function."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 4, 24, 0, 0)

    cached_rows = [
        {
            'time': datetime(2025, 12, 4, h, 0, 0),
            'energy_kwh': 0.15,
            'demand_kw': 20.0,
            'source': 'caiso_dam',
        }
        for h in range(24)
    ]
    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=cached_rows)

    prices = await get_cached_prices(mock_pool, depot_id, start, end)

    assert len(prices) == 24
    assert all(p['energy_kwh'] == 0.15 for p in prices)


@pytest.mark.asyncio
async def test_get_cached_prices_empty(mock_pool):
    """Test get_cached_prices with no cached data."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 4, 24, 0, 0)

    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=[])

    prices = await get_cached_prices(mock_pool, depot_id, start, end)

    assert len(prices) == 0


@pytest.mark.asyncio
async def test_get_latest_price(mock_pool):
    """Test get_latest_price function."""
    depot_id = uuid4()

    latest_row = {
        'time': datetime(2025, 12, 4, 12, 0, 0),
        'energy_kwh': 0.15,
        'demand_kw': 20.0,
        'source': 'caiso_dam',
    }
    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=latest_row)

    price = await get_latest_price(mock_pool, depot_id)

    assert price is not None
    assert price['energy_kwh'] == 0.15
    assert price['time'] == datetime(2025, 12, 4, 12, 0, 0)


@pytest.mark.asyncio
async def test_get_latest_price_none(mock_pool):
    """Test get_latest_price when no prices exist."""
    depot_id = uuid4()

    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=None)

    price = await get_latest_price(mock_pool, depot_id)

    assert price is None


# ============ Error Handling Tests ============


@pytest.mark.asyncio
async def test_store_prices_database_error(mock_pool, sample_caiso_prices):
    """Test store_prices handles database errors."""
    depot_id = uuid4()

    mock_pool.acquire.return_value.execute = AsyncMock(
        side_effect=asyncpg.PostgresError("Database error")
    )

    with pytest.raises(asyncpg.PostgresError):
        await store_prices(mock_pool, sample_caiso_prices[:5], depot_id)


@pytest.mark.asyncio
async def test_get_cached_prices_database_error(mock_pool):
    """Test get_cached_prices handles database errors."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 4, 24, 0, 0)

    mock_pool.acquire.return_value.fetch = AsyncMock(
        side_effect=asyncpg.PostgresError("Database error")
    )

    with pytest.raises(asyncpg.PostgresError):
        await get_cached_prices(mock_pool, depot_id, start, end)


# ============ Price Conversion Tests ============


def test_price_conversion_mwh_to_kwh():
    """Test LMP conversion from $/MWh to $/kWh."""
    price = CAISOPrice(
        timestamp=datetime(2025, 12, 4, 12, 0, 0),
        lmp=150.0,  # $/MWh
        energy=120.0,
        congestion=22.5,
        loss=7.5,
        node="SLAP_PGAE-APND",
    )

    # Conversion: 150.0 $/MWh = 0.15 $/kWh
    energy_kwh = price.lmp / 1000.0
    assert energy_kwh == 0.15


@pytest.mark.asyncio
async def test_store_prices_conversion(mock_pool):
    """Test that prices are converted correctly when stored."""
    depot_id = uuid4()
    prices = [
        CAISOPrice(
            timestamp=datetime(2025, 12, 4, 12, 0, 0),
            lmp=250.0,  # $/MWh
            energy=200.0,
            congestion=37.5,
            loss=12.5,
            node="SLAP_PGAE-APND",
        )
    ]

    await store_prices(mock_pool, prices, depot_id)

    # Verify conversion: 250.0 $/MWh -> 0.25 $/kWh
    call_args = mock_pool.acquire.return_value.execute.call_args[0]
    assert call_args[2] == 0.25  # energy_kwh

