"""Unit tests for Open-Meteo weather adapter.

Reference: Development plan Step 3.3, PRD.md#11-2-unit-test-requirements

Tests the official openmeteo-requests library integration with:
- FlatBuffers response parsing
- Request-level caching (requests-cache)
- Automatic retry mechanism (retry-requests)
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("openmeteo_requests")

import asyncpg
import numpy as np

from src.adapters.weather import (
    OpenMeteoAdapter,
    WeatherData,
    convert_solar_radiation_wm2_to_calcm2,
    get_cached_forecasts,
    get_depot_location,
    get_latest_forecast,
    store_weather_forecasts,
)

# ============ Fixtures ============


@pytest.fixture
def mock_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock()
    conn = MagicMock()

    # Setup async methods on connection
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    # Setup async context manager for pool.acquire()
    # The context manager should return conn when entered
    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=conn)
    async_cm.__aexit__ = AsyncMock(return_value=None)

    # pool.acquire() returns the async context manager
    pool.acquire = MagicMock(return_value=async_cm)

    # Store conn on pool for test access via pool.acquire.return_value.__aenter__.return_value
    # But also add a convenience attribute for tests
    pool._mock_conn = conn

    return pool


@pytest.fixture
def sample_weather_data():
    """Sample weather forecast data."""
    base_time = datetime(2025, 12, 4, 0, 0, 0, tzinfo=timezone.utc)
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
def mock_flatbuffers_response():
    """Mock Open-Meteo FlatBuffers response structure."""

    # Create mock variable objects that return numpy arrays
    def create_mock_variable(values):
        var = MagicMock()
        var.ValuesAsNumpy.return_value = np.array(values, dtype=np.float32)
        return var

    # Create mock daily object
    daily = MagicMock()
    daily.Time.return_value = 1733270400  # 2025-12-04 00:00:00 UTC
    daily.TimeEnd.return_value = 1733529600  # 2025-12-07 00:00:00 UTC (3 days)
    daily.Interval.return_value = 86400  # 1 day in seconds

    # Create mock variables (order matches API request)
    temp_max_var = create_mock_variable([75.0, 77.0, 79.0])
    temp_min_var = create_mock_variable([65.0, 67.0, 69.0])
    precip_var = create_mock_variable([0.0, 0.1, 0.2])
    solar_var = create_mock_variable([500.0, 510.0, 520.0])

    daily.Variables.return_value = [temp_max_var, temp_min_var, precip_var, solar_var]

    # Create mock response
    response = MagicMock()
    response.Latitude.return_value = 37.7749
    response.Longitude.return_value = -122.4194
    response.Elevation.return_value = 16.0
    response.UtcOffsetSeconds.return_value = -28800  # PST
    response.Daily.return_value = daily

    return response


@pytest.fixture
def weather_adapter(mock_pool, tmp_path):
    """OpenMeteo adapter instance with mocked pool."""
    with patch("src.adapters.weather.openmeteo._create_openmeteo_client") as mock_create:
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        adapter = OpenMeteoAdapter(
            latitude=37.7749,
            longitude=-122.4194,
            pool=mock_pool,
            cache_dir=str(tmp_path / "cache"),
        )
        adapter._client = mock_client
        return adapter


# ============ WeatherData Tests ============


def test_weather_data_creation():
    """Test WeatherData dataclass creation."""
    weather = WeatherData(
        timestamp=datetime(2025, 12, 4, 12, 0, 0, tzinfo=timezone.utc),
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
async def test_get_forecast_success(weather_adapter, mock_flatbuffers_response):
    """Test successful forecast fetching with FlatBuffers response."""
    weather_adapter._client.weather_api.return_value = [mock_flatbuffers_response]

    forecasts = await weather_adapter.get_forecast(days=3)

    assert len(forecasts) == 3
    assert forecasts[0].temperature_f == 70.0  # (75 + 65) / 2
    assert forecasts[0].temperature_max_f == 75.0
    assert forecasts[0].temperature_min_f == 65.0
    assert forecasts[0].precipitation_inches == 0.0
    assert forecasts[0].solar_radiation == 500.0

    # Verify second day
    assert forecasts[1].temperature_max_f == 77.0
    assert forecasts[1].precipitation_inches == pytest.approx(0.1, rel=0.01)


@pytest.mark.asyncio
async def test_get_forecast_api_error(weather_adapter):
    """Test forecast fetching with API error."""
    weather_adapter._client.weather_api.side_effect = Exception("API Error")

    with pytest.raises(Exception, match="API Error"):
        await weather_adapter.get_forecast(days=7)


@pytest.mark.asyncio
async def test_get_forecast_empty_response(weather_adapter):
    """Test handling of empty API response."""
    weather_adapter._client.weather_api.return_value = []

    forecasts = await weather_adapter.get_forecast(days=7)

    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_get_forecast_no_daily_data(weather_adapter, mock_flatbuffers_response):
    """Test handling when Daily() returns None."""
    mock_flatbuffers_response.Daily.return_value = None
    weather_adapter._client.weather_api.return_value = [mock_flatbuffers_response]

    forecasts = await weather_adapter.get_forecast(days=7)

    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_get_forecast_max_days(weather_adapter, mock_flatbuffers_response):
    """Test forecast days limited to 16."""
    weather_adapter._client.weather_api.return_value = [mock_flatbuffers_response]

    await weather_adapter.get_forecast(days=20)

    # Verify days parameter was limited to 16
    call_args = weather_adapter._client.weather_api.call_args
    assert call_args[1]["params"]["forecast_days"] == 16


@pytest.mark.asyncio
async def test_get_forecast_uses_list_format(weather_adapter, mock_flatbuffers_response):
    """Test that API parameters use list format per official docs."""
    weather_adapter._client.weather_api.return_value = [mock_flatbuffers_response]

    await weather_adapter.get_forecast(days=7)

    call_args = weather_adapter._client.weather_api.call_args
    params = call_args[1]["params"]

    # Verify 'daily' parameter is a list
    assert isinstance(params["daily"], list)
    assert "temperature_2m_max" in params["daily"]
    assert "temperature_2m_min" in params["daily"]
    assert "precipitation_sum" in params["daily"]
    assert "shortwave_radiation_sum" in params["daily"]


@pytest.mark.asyncio
async def test_get_forecast_nan_handling(weather_adapter):
    """Test that NaN values are replaced with defaults."""

    # Create response with NaN values
    def create_mock_variable(values):
        var = MagicMock()
        var.ValuesAsNumpy.return_value = np.array(values, dtype=np.float32)
        return var

    daily = MagicMock()
    daily.Time.return_value = 1733270400
    daily.TimeEnd.return_value = 1733356800  # 1 day
    daily.Interval.return_value = 86400

    # Include NaN values
    temp_max_var = create_mock_variable([np.nan])
    temp_min_var = create_mock_variable([np.nan])
    precip_var = create_mock_variable([np.nan])
    solar_var = create_mock_variable([np.nan])

    daily.Variables.return_value = [temp_max_var, temp_min_var, precip_var, solar_var]

    response = MagicMock()
    response.Latitude.return_value = 37.7749
    response.Longitude.return_value = -122.4194
    response.Elevation.return_value = 16.0
    response.UtcOffsetSeconds.return_value = 0
    response.Daily.return_value = daily

    weather_adapter._client.weather_api.return_value = [response]

    forecasts = await weather_adapter.get_forecast(days=1)

    assert len(forecasts) == 1
    # Should use default values for NaN
    assert forecasts[0].temperature_max_f == 75.0
    assert forecasts[0].temperature_min_f == 65.0
    assert forecasts[0].precipitation_inches == 0.0
    assert forecasts[0].solar_radiation == 500.0


@pytest.mark.asyncio
async def test_store_forecasts_to_db(weather_adapter, sample_weather_data, mock_pool):
    """Test storing forecasts to database."""
    depot_id = uuid4()
    stored_count = await weather_adapter.store_forecasts_to_db(sample_weather_data[:5], depot_id)

    assert stored_count == 5
    assert mock_pool._mock_conn.execute.call_count == 5


@pytest.mark.asyncio
async def test_store_forecasts_to_db_no_pool(sample_weather_data, tmp_path):
    """Test storing forecasts without pool raises error."""
    with patch("src.adapters.weather.openmeteo._create_openmeteo_client"):
        adapter = OpenMeteoAdapter(
            latitude=37.7749,
            longitude=-122.4194,
            cache_dir=str(tmp_path / "cache"),
        )  # No pool
    depot_id = uuid4()

    with pytest.raises(RuntimeError, match="Database pool not configured"):
        await adapter.store_forecasts_to_db([sample_weather_data[0]], depot_id)


@pytest.mark.asyncio
async def test_get_forecasts_for_depot_with_cache(weather_adapter, sample_weather_data, mock_pool):
    """Test getting forecasts with cache hit."""
    depot_id = uuid4()

    # Mock cached forecasts
    cached_rows = [
        {
            "time": w.timestamp,
            "temp_f": w.temperature_f,
            "temp_max_f": w.temperature_max_f,
            "temp_min_f": w.temperature_min_f,
            "precip_in": w.precipitation_inches,
            "solar_rad": convert_solar_radiation_wm2_to_calcm2(w.solar_radiation),
        }
        for w in sample_weather_data[:5]
    ]
    mock_pool._mock_conn.fetch = AsyncMock(return_value=cached_rows)

    # Mock depot location
    mock_pool._mock_conn.fetchrow = AsyncMock(
        return_value={"latitude": 37.7749, "longitude": -122.4194}
    )

    forecasts = await weather_adapter.get_forecasts_for_depot(depot_id, days=7, use_cache=True)

    assert len(forecasts) == 5
    # Verify forecasts were converted from cache
    assert all(f.temperature_f > 0 for f in forecasts)


@pytest.mark.asyncio
async def test_get_forecasts_for_depot_no_cache(
    weather_adapter, mock_pool, mock_flatbuffers_response
):
    """Test getting forecasts without cache (fresh fetch)."""
    depot_id = uuid4()

    # Mock no cached forecasts
    mock_pool._mock_conn.fetch = AsyncMock(return_value=[])
    mock_pool._mock_conn.fetchrow = AsyncMock(
        return_value={"latitude": 37.7749, "longitude": -122.4194}
    )

    weather_adapter._client.weather_api.return_value = [mock_flatbuffers_response]

    forecasts = await weather_adapter.get_forecasts_for_depot(depot_id, days=7, use_cache=False)

    assert len(forecasts) == 3
    # Verify forecasts were stored
    assert mock_pool._mock_conn.execute.call_count > 0


@pytest.mark.asyncio
async def test_get_forecasts_for_depot_no_location(weather_adapter, mock_pool):
    """Test getting forecasts when depot location not found."""
    depot_id = uuid4()

    # Set latitude/longitude to None to trigger location lookup
    weather_adapter.latitude = None
    weather_adapter.longitude = None
    mock_pool._mock_conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="Could not find location"):
        await weather_adapter.get_forecasts_for_depot(depot_id, days=7)


# ============ Client Creation Tests ============


def test_create_openmeteo_client(tmp_path):
    """Test that client is created with caching and retry."""
    from src.adapters.weather.openmeteo import _create_openmeteo_client

    with patch("src.adapters.weather.openmeteo.requests_cache.CachedSession") as mock_cache:
        with patch("src.adapters.weather.openmeteo.retry") as mock_retry:
            with patch("src.adapters.weather.openmeteo.openmeteo_requests.Client") as mock_client:
                mock_cache_session = MagicMock()
                mock_cache.return_value = mock_cache_session
                mock_retry_session = MagicMock()
                mock_retry.return_value = mock_retry_session

                _create_openmeteo_client(
                    cache_dir=str(tmp_path),
                    cache_expire_after=3600,
                    retries=5,
                    backoff_factor=0.2,
                )

                # Verify cache was created with correct parameters
                mock_cache.assert_called_once()
                cache_call = mock_cache.call_args
                assert cache_call[0][0] == str(tmp_path)
                assert cache_call[1]["expire_after"] == 3600
                assert cache_call[1]["backend"] == "sqlite"

                # Verify retry was configured
                mock_retry.assert_called_once_with(
                    mock_cache_session,
                    retries=5,
                    backoff_factor=0.2,
                )

                # Verify client was created with retry session
                mock_client.assert_called_once_with(session=mock_retry_session)


def test_adapter_initialization_creates_cache_dir(tmp_path):
    """Test that adapter creates cache directory if it doesn't exist."""
    cache_dir = tmp_path / "new_cache_dir"
    assert not cache_dir.exists()

    with patch("src.adapters.weather.openmeteo.openmeteo_requests.Client"):
        with patch("src.adapters.weather.openmeteo.requests_cache.CachedSession"):
            with patch("src.adapters.weather.openmeteo.retry"):
                OpenMeteoAdapter(
                    latitude=37.7749,
                    longitude=-122.4194,
                    cache_dir=str(cache_dir),
                )

    assert cache_dir.exists()


# ============ Storage Function Tests ============


@pytest.mark.asyncio
async def test_store_weather_forecasts(mock_pool, sample_weather_data):
    """Test store_weather_forecasts function."""
    depot_id = uuid4()
    stored_count = await store_weather_forecasts(mock_pool, sample_weather_data[:7], depot_id)

    assert stored_count == 7
    # Verify execute was called for each forecast
    assert mock_pool._mock_conn.execute.call_count == 7


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
    # call_args[0] is positional args: (query, timestamp, depot_id, temp_f, temp_max_f, temp_min_f, precip, solar_rad)
    call_args = mock_pool._mock_conn.execute.call_args[0]
    # solar_rad is at index 7 (0=query, 1=timestamp, 2=depot_id, 3=temp_f, 4=temp_max, 5=temp_min, 6=precip, 7=solar)
    assert call_args[7] == pytest.approx(1032.0, rel=0.01)


@pytest.mark.asyncio
async def test_get_cached_forecasts(mock_pool):
    """Test get_cached_forecasts function."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)

    cached_rows = [
        {
            "time": datetime(2025, 12, 4, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=d),
            "temp_f": 70.0 + d,
            "temp_max_f": 75.0 + d,
            "temp_min_f": 65.0 + d,
            "precip_in": 0.1 * d,
            "solar_rad": 1000.0 + d * 10,
        }
        for d in range(7)
    ]
    mock_pool._mock_conn.fetch = AsyncMock(return_value=cached_rows)

    forecasts = await get_cached_forecasts(mock_pool, depot_id, start, end)

    assert len(forecasts) == 7
    assert all(f["temp_f"] > 0 for f in forecasts)


@pytest.mark.asyncio
async def test_get_cached_forecasts_empty(mock_pool):
    """Test get_cached_forecasts with no cached data."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)

    mock_pool._mock_conn.fetch = AsyncMock(return_value=[])

    forecasts = await get_cached_forecasts(mock_pool, depot_id, start, end)

    assert len(forecasts) == 0


@pytest.mark.asyncio
async def test_get_latest_forecast(mock_pool):
    """Test get_latest_forecast function."""
    depot_id = uuid4()

    latest_row = {
        "time": datetime(2025, 12, 4, 12, 0, 0, tzinfo=timezone.utc),
        "temp_f": 70.0,
        "temp_max_f": 75.0,
        "temp_min_f": 65.0,
        "precip_in": 0.1,
        "solar_rad": 1000.0,
    }
    mock_pool._mock_conn.fetchrow = AsyncMock(return_value=latest_row)

    forecast = await get_latest_forecast(mock_pool, depot_id)

    assert forecast is not None
    assert forecast["temp_f"] == 70.0
    assert forecast["time"] == datetime(2025, 12, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_get_latest_forecast_none(mock_pool):
    """Test get_latest_forecast when no forecasts exist."""
    depot_id = uuid4()

    mock_pool._mock_conn.fetchrow = AsyncMock(return_value=None)

    forecast = await get_latest_forecast(mock_pool, depot_id)

    assert forecast is None


@pytest.mark.asyncio
async def test_get_depot_location(mock_pool):
    """Test get_depot_location function."""
    depot_id = uuid4()

    location_row = {"latitude": 37.7749, "longitude": -122.4194}
    mock_pool._mock_conn.fetchrow = AsyncMock(return_value=location_row)

    location = await get_depot_location(mock_pool, depot_id)

    assert location == (37.7749, -122.4194)


@pytest.mark.asyncio
async def test_get_depot_location_none(mock_pool):
    """Test get_depot_location when depot not found."""
    depot_id = uuid4()

    mock_pool._mock_conn.fetchrow = AsyncMock(return_value=None)

    location = await get_depot_location(mock_pool, depot_id)

    assert location is None


@pytest.mark.asyncio
async def test_get_depot_location_missing_coords(mock_pool):
    """Test get_depot_location when coordinates are null."""
    depot_id = uuid4()

    location_row = {"latitude": None, "longitude": None}
    mock_pool._mock_conn.fetchrow = AsyncMock(return_value=location_row)

    location = await get_depot_location(mock_pool, depot_id)

    assert location is None


# ============ Error Handling Tests ============


@pytest.mark.asyncio
async def test_store_weather_forecasts_database_error(mock_pool, sample_weather_data):
    """Test store_weather_forecasts handles database errors."""
    depot_id = uuid4()

    mock_pool._mock_conn.execute = AsyncMock(side_effect=asyncpg.PostgresError("Database error"))

    with pytest.raises(asyncpg.PostgresError):
        await store_weather_forecasts(mock_pool, sample_weather_data[:5], depot_id)


@pytest.mark.asyncio
async def test_get_cached_forecasts_database_error(mock_pool):
    """Test get_cached_forecasts handles database errors."""
    depot_id = uuid4()
    start = datetime(2025, 12, 4, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)

    mock_pool._mock_conn.fetch = AsyncMock(side_effect=asyncpg.PostgresError("Database error"))

    with pytest.raises(asyncpg.PostgresError):
        await get_cached_forecasts(mock_pool, depot_id, start, end)


# ============ Cache Management Tests ============


def test_clear_cache(tmp_path):
    """Test cache clearing functionality."""
    # requests_cache creates cache file at {cache_dir}.sqlite (sibling to directory)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = tmp_path / "cache.sqlite"  # Sibling to directory, not inside
    cache_file.write_text("fake cache data")

    with patch("src.adapters.weather.openmeteo._create_openmeteo_client"):
        adapter = OpenMeteoAdapter(
            latitude=37.7749,
            longitude=-122.4194,
            cache_dir=str(cache_dir),
        )

    assert cache_file.exists()
    adapter.clear_cache()
    assert not cache_file.exists()


@pytest.mark.asyncio
async def test_close_adapter(weather_adapter):
    """Test adapter close method."""
    assert weather_adapter._client is not None
    await weather_adapter.close()
    assert weather_adapter._client is None


# ============ Integration-style Tests ============


@pytest.mark.asyncio
async def test_full_forecast_workflow(weather_adapter, mock_pool, mock_flatbuffers_response):
    """Test complete workflow: fetch -> store -> retrieve from cache."""
    depot_id = uuid4()

    # Setup mocks
    weather_adapter._client.weather_api.return_value = [mock_flatbuffers_response]
    mock_pool._mock_conn.fetch = AsyncMock(return_value=[])
    mock_pool._mock_conn.fetchrow = AsyncMock(
        return_value={"latitude": 37.7749, "longitude": -122.4194}
    )

    # First call: should fetch from API
    forecasts = await weather_adapter.get_forecasts_for_depot(depot_id, days=3, use_cache=False)

    assert len(forecasts) == 3
    assert weather_adapter._client.weather_api.called
    # Verify storage was called
    assert mock_pool._mock_conn.execute.call_count == 3

    # Reset mock
    mock_pool._mock_conn.execute.reset_mock()
    weather_adapter._client.weather_api.reset_mock()

    # Setup cache return
    cached_rows = [
        {
            "time": f.timestamp,
            "temp_f": f.temperature_f,
            "temp_max_f": f.temperature_max_f,
            "temp_min_f": f.temperature_min_f,
            "precip_in": f.precipitation_inches,
            "solar_rad": convert_solar_radiation_wm2_to_calcm2(f.solar_radiation),
        }
        for f in forecasts
    ]
    mock_pool._mock_conn.fetch = AsyncMock(return_value=cached_rows)

    # Second call: should use cache
    cached_forecasts = await weather_adapter.get_forecasts_for_depot(
        depot_id, days=3, use_cache=True
    )

    assert len(cached_forecasts) == 3
    # API should not have been called again
    assert not weather_adapter._client.weather_api.called
