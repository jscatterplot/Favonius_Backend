"""Unit tests for Open-Meteo weather adapter.

Reference: Development plan Step 3.3, PRD.md#11-2-unit-test-requirements
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import httpx
import pytest

from src.adapters.weather import (
    OpenMeteoAdapter,
    WeatherData,
    convert_solar_radiation_wm2_to_calcm2,
    get_cached_forecasts,
    get_depot_location,
    store_weather_forecasts,
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
def sample_weather_data():
    """Sample weather forecast data."""
    base_time = datetime(2025, 12, 4, 0, 0, 0)
    forecasts = []
    for day in range(7):
        forecasts.append(
            WeatherData(
                timestamp=base_time + timedelta(days=day),
                temperature_f=70.0 + day * 2,
                temperature_max_f=75.0 + day * 2,
                temperature_min_f=65.0 + day * 2,
                precipitation_inches=0.1 * day,
                solar_radiation=500.0 + day * 10,  # W/m²
            )
        )
    return forecasts


@pytest.fixture
def mock_api_response():
    """Mock Open-Meteo API response."""
    return {
        'daily': {
            'time': [
                '2025-12-04',
                '2025-12-05',
                '2025-12-06',
            ],
            'temperature_2m_max': [75.0, 77.0, 79.0],
            'temperature_2m_min': [65.0, 67.0, 69.0],
            'precipitation_sum': [0.0, 0.1, 0.2],
            'shortwave_radiation_sum': [500.0, 510.0, 520.0],
        },
        'hourly': {
            'time': [],
            'temperature_2m': [],
        },
    }


@pytest.fixture
def weather_adapter(mock_pool):
    """OpenMeteo adapter instance with mocked pool."""
    return OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=mock_pool)


# ============ WeatherData Tests ============


def test_weather_data_creation():
    """Test WeatherData dataclass creation."""
    weather = WeatherData(
        timestamp=datetime(2025, 12, 4, 12, 0, 0),
        temperature_f=70.0,
        temperature_max_f=75.0,
        temperature_min_f=65.0,
        precipitation_inches=0.1,
        solar_radiation=500.0,
    )
    assert weather.temperature_f == 70.0
    assert weather.solar_radiation == 500.0


# ============ Solar Radiation Conversion Tests ============


def test_solar_radiation_conversion():
    """Test W/m² to cal/cm² conversion."""
    # 500 W/m² should convert to approximately 1032 cal/cm²
    result = convert_solar_radiation_wm2_to_calcm2(500.0)
    expected = 500.0 * 2.064
    assert abs(result - expected) < 0.01


def test_solar_radiation_conversion_zero():
    """Test conversion with zero input."""
    result = convert_solar_radiation_wm2_to_calcm2(0.0)
    assert result == 0.0


# ============ OpenMeteoAdapter Tests ============


@pytest.mark.asyncio
async def test_get_forecast_success(weather_adapter, mock_api_response):
    """Test successful forecast fetching."""
    with patch.object(weather_adapter.client, 'get') as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = mock_api_response
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        forecasts = await weather_adapter.get_forecast(days=3)

        assert len(forecasts) == 3
        assert forecasts[0].temperature_f == 70.0  # (75 + 65) / 2
        assert forecasts[0].temperature_max_f == 75.0
        assert forecasts[0].precipitation_inches == 0.0


@pytest.mark.asyncio
async def test_get_forecast_api_error(weather_adapter):
    """Test forecast fetching with API error."""
    with patch.object(weather_adapter.client, 'get') as mock_get:
        mock_get.side_effect = httpx.HTTPStatusError(
            "API Error", request=MagicMock(), response=MagicMock()
        )

        with pytest.raises(httpx.HTTPStatusError):
            await weather_adapter.get_forecast(days=7)


@pytest.mark.asyncio
async def test_get_forecast_max_days(weather_adapter):
    """Test forecast days limited to 16."""
    with patch.object(weather_adapter.client, 'get') as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = {'daily': {'time': []}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        await weather_adapter.get_forecast(days=20)

        # Verify days parameter was limited to 16
        call_args = mock_get.call_args
        assert call_args[1]['params']['forecast_days'] == 16


@pytest.mark.asyncio
async def test_store_forecasts_to_db(weather_adapter, sample_weather_data, mock_pool):
    """Test storing forecasts to database."""
    depot_id = uuid4()
    stored_count = await weather_adapter.store_forecasts_to_db(
        sample_weather_data[:5], depot_id
    )

    assert stored_count == 5
    assert mock_pool.acquire.return_value.execute.call_count == 5


@pytest.mark.asyncio
async def test_store_forecasts_to_db_no_pool(sample_weather_data):
    """Test storing forecasts without pool raises error."""
    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194)  # No pool
    depot_id = uuid4()

    with pytest.raises(RuntimeError, match="Database pool not configured"):
        await adapter.store_forecasts_to_db([sample_weather_data[0]], depot_id)


@pytest.mark.asyncio
async def test_get_forecasts_for_depot_with_cache(
    weather_adapter, sample_weather_data, mock_pool
):
    """Test getting forecasts with cache hit."""
    depot_id = uuid4()

    # Mock cached forecasts
    cached_rows = [
        {
            'time': w.timestamp,
            'temp_f': w.temperature_f,
            'temp_max_f': w.temperature_max_f,
            'temp_min_f': w.temperature_min_f,
            'precip_in': w.precipitation_inches,
            'solar_rad': convert_solar_radiation_wm2_to_calcm2(w.solar_radiation),
        }
        for w in sample_weather_data[:5]
    ]
    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=cached_rows)

    # Mock depot location
    mock_pool.acquire.return_value.fetchrow = AsyncMock(
        return_value={'latitude': 37.7749, 'longitude': -122.4194}
    )

    forecasts = await weather_adapter.get_forecasts_for_depot(
        depot_id, days=7, use_cache=True
    )

    assert len(forecasts) == 5
    # Verify forecasts were converted from cache
    assert all(f.temperature_f > 0 for f in forecasts)


@pytest.mark.asyncio
async def test_get_forecasts_for_depot_no_cache(weather_adapter, mock_pool, mock_api_response):
    """Test getting forecasts without cache (fresh fetch)."""
    depot_id = uuid4()

    # Mock no cached forecasts
    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=[])
    mock_pool.acquire.return_value.fetchrow = AsyncMock(
        return_value={'latitude': 37.7749, 'longitude': -122.4194}
    )

    with patch.object(weather_adapter.client, 'get') as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = mock_api_response
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        forecasts = await weather_adapter.get_forecasts_for_depot(
            depot_id, days=7, use_cache=False
        )

        assert len(forecasts) == 3
        # Verify forecasts were stored
        assert mock_pool.acquire.return_value.execute.call_count > 0


@pytest.mark.asyncio
async def test_get_forecasts_for_depot_no_location(weather_adapter, mock_pool):
    """Test getting forecasts when depot location not found."""
    depot_id = uuid4()

    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="Could not find location"):
        await weather_adapter.get_forecasts_for_depot(depot_id, days=7)


# ============ Storage Function Tests ============


@pytest.mark.asyncio
async def test_store_weather_forecasts(mock_pool, sample_weather_data):
    """Test store_weather_forecasts function."""
    depot_id = uuid4()
    stored_count = await store_weather_forecasts(
        mock_pool, sample_weather_data[:10], depot_id
    )

    assert stored_count == 10
    # Verify execute was called for each forecast
    assert mock_pool.acquire.return_value.execute.call_count == 10


@pytest.mark.asyncio
async def test_store_weather_forecasts_empty_list(mock_pool):
    """Test store_weather_forecasts with empty list."""
    stored_count = await store_weather_forecasts(mock_pool, [], uuid4())
    assert stored_count == 0


@pytest.mark.asyncio
async def test_store_weather_forecasts_solar_conversion(mock_pool, sample_weather_data):
    """Test that solar radiation is converted when storing."""
    depot_id = uuid4()
    await store_weather_forecasts(mock_pool, sample_weather_data[:1], depot_id)

    # Verify conversion was applied in execute call
    call_args = mock_pool.acquire.return_value.execute.call_args[0]
    # solar_rad should be converted value (500.0 * 2.064 = 1032.0)
    assert call_args[6] == pytest.approx(1032.0, rel=0.01)


@pytest.mark.asyncio
async def test_get_cached_forecasts(mock_pool):
    """Test get_cached_forecasts function."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 11, 0, 0, 0)

    cached_rows = [
        {
            'time': datetime(2025, 12, 4, 0, 0, 0) + timedelta(days=d),
            'temp_f': 70.0 + d,
            'temp_max_f': 75.0 + d,
            'temp_min_f': 65.0 + d,
            'precip_in': 0.1 * d,
            'solar_rad': 1000.0 + d * 10,
        }
        for d in range(7)
    ]
    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=cached_rows)

    forecasts = await get_cached_forecasts(mock_pool, depot_id, start, end)

    assert len(forecasts) == 7
    assert all(f['temp_f'] > 0 for f in forecasts)


@pytest.mark.asyncio
async def test_get_cached_forecasts_empty(mock_pool):
    """Test get_cached_forecasts with no cached data."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 11, 0, 0, 0)

    mock_pool.acquire.return_value.fetch = AsyncMock(return_value=[])

    forecasts = await get_cached_forecasts(mock_pool, depot_id, start, end)

    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_get_latest_forecast(mock_pool):
    """Test get_latest_forecast function."""
    depot_id = uuid4()

    latest_row = {
        'time': datetime(2025, 12, 4, 12, 0, 0),
        'temp_f': 70.0,
        'temp_max_f': 75.0,
        'temp_min_f': 65.0,
        'precip_in': 0.1,
        'solar_rad': 1000.0,
    }
    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=latest_row)

    forecast = await get_latest_forecast(mock_pool, depot_id)

    assert forecast is not None
    assert forecast['temp_f'] == 70.0
    assert forecast['time'] == datetime(2025, 12, 4, 12, 0, 0)


@pytest.mark.asyncio
async def test_get_latest_forecast_none(mock_pool):
    """Test get_latest_forecast when no forecasts exist."""
    depot_id = uuid4()

    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=None)

    forecast = await get_latest_forecast(mock_pool, depot_id)

    assert forecast is None


@pytest.mark.asyncio
async def test_get_depot_location(mock_pool):
    """Test get_depot_location function."""
    depot_id = uuid4()

    location_row = {'latitude': 37.7749, 'longitude': -122.4194}
    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=location_row)

    location = await get_depot_location(mock_pool, depot_id)

    assert location == (37.7749, -122.4194)


@pytest.mark.asyncio
async def test_get_depot_location_none(mock_pool):
    """Test get_depot_location when depot not found."""
    depot_id = uuid4()

    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=None)

    location = await get_depot_location(mock_pool, depot_id)

    assert location is None


@pytest.mark.asyncio
async def test_get_depot_location_missing_coords(mock_pool):
    """Test get_depot_location when coordinates are null."""
    depot_id = uuid4()

    location_row = {'latitude': None, 'longitude': None}
    mock_pool.acquire.return_value.fetchrow = AsyncMock(return_value=location_row)

    location = await get_depot_location(mock_pool, depot_id)

    assert location is None


# ============ Error Handling Tests ============


@pytest.mark.asyncio
async def test_store_weather_forecasts_database_error(mock_pool, sample_weather_data):
    """Test store_weather_forecasts handles database errors."""
    depot_id = uuid4()

    mock_pool.acquire.return_value.execute = AsyncMock(
        side_effect=asyncpg.PostgresError("Database error")
    )

    with pytest.raises(asyncpg.PostgresError):
        await store_weather_forecasts(mock_pool, sample_weather_data[:5], depot_id)


@pytest.mark.asyncio
async def test_get_cached_forecasts_database_error(mock_pool):
    """Test get_cached_forecasts handles database errors."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0)
    end = datetime(2025, 12, 11, 0, 0, 0)

    mock_pool.acquire.return_value.fetch = AsyncMock(
        side_effect=asyncpg.PostgresError("Database error")
    )

    with pytest.raises(asyncpg.PostgresError):
        await get_cached_forecasts(mock_pool, depot_id, start, end)


# ============ Data Parsing Tests ============


@pytest.mark.asyncio
async def test_parse_api_response_missing_fields(weather_adapter):
    """Test parsing API response with missing fields."""
    incomplete_response = {
        'daily': {
            'time': ['2025-12-04'],
            'temperature_2m_max': [75.0],
            # Missing other fields
        },
    }

    with patch.object(weather_adapter.client, 'get') as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = incomplete_response
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        forecasts = await weather_adapter.get_forecast(days=1)

        assert len(forecasts) == 1
        # Should use defaults for missing fields
        assert forecasts[0].temperature_min_f == 65.0  # Default
        assert forecasts[0].precipitation_inches == 0.0  # Default

