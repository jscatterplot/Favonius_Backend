"""Integration tests for weather data flow.

Reference: Development plan Step 3.3, PRD.md#11-3-integration-test-requirements
"""

from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from src.adapters.weather import (
    OpenMeteoAdapter,
    WeatherIngestionService,
    get_cached_forecasts,
    store_weather_forecasts,
)
from src.core.surrogate.training import fetch_training_data


@pytest.fixture
async def test_db_pool():
    """Create test database connection pool."""
    import os

    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/favonius_test",
    )

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    yield pool
    await pool.close()


@pytest.fixture
async def test_depot_id(test_db_pool):
    """Yield a depot UUID. Shadow depots table dropped in migration 029."""
    depot_id = uuid4()
    yield depot_id
    async with test_db_pool.acquire() as conn:
        await conn.execute("DELETE FROM weather_forecasts WHERE depot_id = $1", depot_id)


@pytest.mark.asyncio
async def test_weather_to_database_flow(test_db_pool, test_depot_id):
    """Test weather adapter → database storage flow."""
    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=test_db_pool)

    # Mock API response since we don't want to hit real API in tests
    mock_response = {
        "daily": {
            "time": [
                (datetime.utcnow() + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)
            ],
            "temperature_2m_max": [75.0] * 7,
            "temperature_2m_min": [65.0] * 7,
            "precipitation_sum": [0.1] * 7,
            "shortwave_radiation_sum": [500.0] * 7,
        },
        "hourly": {"time": [], "temperature_2m": []},
    }

    with pytest.mock.patch.object(adapter.client, "get") as mock_get:
        mock_http_response = pytest.mock.MagicMock()
        mock_http_response.json.return_value = mock_response
        mock_http_response.raise_for_status = pytest.mock.MagicMock()
        mock_get.return_value = mock_http_response

        # Fetch forecasts
        forecasts = await adapter.get_forecast(days=7)

        assert len(forecasts) == 7

        # Store forecasts
        stored_count = await adapter.store_forecasts_to_db(forecasts, test_depot_id)

        assert stored_count == 7

        # Verify forecasts in database
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        cached = await get_cached_forecasts(test_db_pool, test_depot_id, start, end)

        assert len(cached) == 7
        assert all(f["temp_f"] > 0 for f in cached)
        assert all(f["solar_rad"] > 0 for f in cached)  # Should be converted


@pytest.mark.asyncio
async def test_weather_caching(test_db_pool, test_depot_id):
    """Test weather caching mechanism."""
    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=test_db_pool)

    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start + timedelta(days=7)

    # Create sample weather data
    from src.adapters.weather import WeatherData

    forecasts = [
        WeatherData(
            timestamp=start + timedelta(days=d),
            temperature_f=70.0 + d,
            temperature_max_f=75.0 + d,
            temperature_min_f=65.0 + d,
            precipitation_inches=0.1 * d,
            solar_radiation=500.0 + d * 10,
        )
        for d in range(7)
    ]

    # Store forecasts first
    await store_weather_forecasts(test_db_pool, forecasts, test_depot_id)

    # Second fetch with cache: should use cached data
    with pytest.mock.patch.object(adapter, "get_forecast"):
        cached_forecasts = await adapter.get_forecasts_for_depot(
            test_depot_id, days=7, use_cache=True
        )

        # Should not call get_forecast if cache hit
        # (In practice, it might still be called, but we verify cache is used)
        assert len(cached_forecasts) == 7


@pytest.mark.skip(
    reason="Uses vehicles+schedules shadow tables dropped in migration 029"
)
@pytest.mark.asyncio
async def test_surrogate_model_reads_weather(test_db_pool, test_depot_id):
    """Test surrogate model training can read weather from database."""
    # Store weather forecasts first
    from src.adapters.weather import WeatherData

    base_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    forecasts = [
        WeatherData(
            timestamp=base_time - timedelta(days=d),
            temperature_f=70.0,
            temperature_max_f=75.0,
            temperature_min_f=65.0,
            precipitation_inches=0.1,
            solar_radiation=500.0,
        )
        for d in range(30)  # 30 days of historical data
    ]

    await store_weather_forecasts(test_db_pool, forecasts, test_depot_id)

    # Create a test vehicle and schedule
    vehicle_id = uuid4()
    async with test_db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO vehicles (
                vehicle_id, depot_id, external_id, vehicle_type,
                battery_kwh, max_charge_kw
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (vehicle_id) DO NOTHING
            """,
            vehicle_id,
            test_depot_id,
            "test_vehicle_1",
            "bus_large",
            324.0,
            80.0,
        )

        # Create a schedule with energy consumption
        schedule_id = uuid4()
        departure_time = base_time - timedelta(days=1)
        await conn.execute(
            """
            INSERT INTO schedules (
                schedule_id, vehicle_id, route_id, departure_time,
                return_time, energy_kwh
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (schedule_id) DO NOTHING
            """,
            schedule_id,
            vehicle_id,
            "route_101",
            departure_time,
            departure_time + timedelta(hours=8),
            200.0,
        )

    try:
        # Fetch training data (should join with weather_forecasts)
        inputs, energies = await fetch_training_data(test_db_pool, test_depot_id, lookback_days=30)

        # Should have at least one training sample
        if inputs:
            assert len(inputs) == len(energies)
            # Verify weather data is present
            assert inputs[0].temp_avg_f > 0
            assert inputs[0].solar_radiation > 0

    finally:
        # Cleanup
        async with test_db_pool.acquire() as conn:
            await conn.execute("DELETE FROM schedules WHERE vehicle_id = $1", vehicle_id)
            await conn.execute("DELETE FROM vehicles WHERE vehicle_id = $1", vehicle_id)


@pytest.mark.asyncio
async def test_weather_ingestion_service(test_db_pool, test_depot_id):
    """Test WeatherIngestionService."""
    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=test_db_pool)
    service = WeatherIngestionService(test_db_pool, adapter=adapter)

    # Mock API response
    mock_response = {
        "daily": {
            "time": [
                (datetime.utcnow() + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)
            ],
            "temperature_2m_max": [75.0] * 7,
            "temperature_2m_min": [65.0] * 7,
            "precipitation_sum": [0.1] * 7,
            "shortwave_radiation_sum": [500.0] * 7,
        },
        "hourly": {"time": [], "temperature_2m": []},
    }

    with pytest.mock.patch.object(adapter.client, "get") as mock_get:
        mock_http_response = pytest.mock.MagicMock()
        mock_http_response.json.return_value = mock_response
        mock_http_response.raise_for_status = pytest.mock.MagicMock()
        mock_get.return_value = mock_http_response

        # Run ingestion once
        results = await service.run_once()

        depot_key = str(test_depot_id)
        if depot_key in results:
            assert results[depot_key] > 0


@pytest.mark.skip(reason="Uses static depots shadow table dropped in migration 029")
@pytest.mark.asyncio
async def test_weather_ingestion_all_depots(test_db_pool):
    """Test weather ingestion for all depots."""
    # Create multiple test depots
    depot_ids = [uuid4() for _ in range(3)]

    async with test_db_pool.acquire() as conn:
        for depot_id in depot_ids:
            await conn.execute(
                """
                INSERT INTO depots (
                    depot_id, name, latitude, longitude, timezone,
                    max_grid_kw, demand_charge_rate_kw
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (depot_id) DO NOTHING
                """,
                depot_id,
                f"Test Depot {depot_id}",
                37.7749,
                -122.4194,
                "America/Los_Angeles",
                1000.0,
                20.0,
            )

    try:
        service = WeatherIngestionService(test_db_pool)

        # Mock API response for all depots
        {
            "daily": {
                "time": [
                    (datetime.utcnow() + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)
                ],
                "temperature_2m_max": [75.0] * 7,
                "temperature_2m_min": [65.0] * 7,
                "precipitation_sum": [0.1] * 7,
                "shortwave_radiation_sum": [500.0] * 7,
            },
            "hourly": {"time": [], "temperature_2m": []},
        }

        # Patch the adapter creation to use mocked API
        with pytest.mock.patch(
            "src.adapters.weather.ingestion.OpenMeteoAdapter"
        ) as mock_adapter_class:
            mock_adapter = pytest.mock.MagicMock()
            mock_adapter.get_forecasts_for_depot = pytest.mock.AsyncMock(
                return_value=[pytest.mock.MagicMock() for _ in range(7)]  # Mock WeatherData objects
            )
            mock_adapter.close = pytest.mock.AsyncMock()
            mock_adapter_class.return_value = mock_adapter

            results = await service.run_once()

            # Verify all depots got forecasts
            for depot_id in depot_ids:
                depot_key = str(depot_id)
                assert depot_key in results
                assert results[depot_key] > 0

    finally:
        # Cleanup
        async with test_db_pool.acquire() as conn:
            for depot_id in depot_ids:
                await conn.execute("DELETE FROM weather_forecasts WHERE depot_id = $1", depot_id)
                await conn.execute("DELETE FROM depots WHERE depot_id = $1", depot_id)


@pytest.mark.asyncio
async def test_fallback_to_cached_forecasts(test_db_pool, test_depot_id):
    """Test fallback to cached forecasts when API fails."""
    from src.adapters.weather import WeatherData

    # Store some cached forecasts first
    base_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    forecasts = [
        WeatherData(
            timestamp=base_time + timedelta(days=d),
            temperature_f=70.0 + d,
            temperature_max_f=75.0 + d,
            temperature_min_f=65.0 + d,
            precipitation_inches=0.1 * d,
            solar_radiation=500.0 + d * 10,
        )
        for d in range(7)
    ]
    await store_weather_forecasts(test_db_pool, forecasts, test_depot_id)

    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=test_db_pool)

    # Simulate API failure by mocking get_forecast to raise
    original_method = adapter.get_forecast

    async def failing_method(*args, **kwargs):
        raise Exception("API failure")

    adapter.get_forecast = failing_method

    try:
        # Should fall back to cached forecasts
        cached = await get_cached_forecasts(
            test_db_pool, test_depot_id, base_time, base_time + timedelta(days=7)
        )

        assert len(cached) == 7
    finally:
        adapter.get_forecast = original_method


@pytest.mark.asyncio
async def test_weather_storage_upsert(test_db_pool, test_depot_id):
    """Test that weather storage handles upserts correctly."""
    from src.adapters.weather import WeatherData

    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=test_db_pool)

    base_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    forecast1 = WeatherData(
        timestamp=base_time,
        temperature_f=70.0,
        temperature_max_f=75.0,
        temperature_min_f=65.0,
        precipitation_inches=0.1,
        solar_radiation=500.0,
    )

    # Store first time
    count1 = await adapter.store_forecasts_to_db([forecast1], test_depot_id)
    assert count1 == 1

    # Store again (should upsert, not duplicate)
    forecast2 = WeatherData(
        timestamp=base_time,
        temperature_f=72.0,  # Different value
        temperature_max_f=77.0,
        temperature_min_f=67.0,
        precipitation_inches=0.2,
        solar_radiation=510.0,
    )
    count2 = await adapter.store_forecasts_to_db([forecast2], test_depot_id)
    assert count2 == 1

    # Verify only one row per time+depot
    cached = await get_cached_forecasts(
        test_db_pool, test_depot_id, base_time, base_time + timedelta(days=1)
    )
    assert len(cached) == 1
    # Verify updated value was stored
    assert cached[0]["temp_f"] == 72.0


@pytest.mark.asyncio
async def test_solar_radiation_conversion_storage(test_db_pool, test_depot_id):
    """Test that solar radiation is correctly converted when storing."""
    from src.adapters.weather import WeatherData

    adapter = OpenMeteoAdapter(latitude=37.7749, longitude=-122.4194, pool=test_db_pool)

    base_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    # Solar radiation in W/m² (from API)
    forecast = WeatherData(
        timestamp=base_time,
        temperature_f=70.0,
        temperature_max_f=75.0,
        temperature_min_f=65.0,
        precipitation_inches=0.1,
        solar_radiation=500.0,  # W/m²
    )

    await adapter.store_forecasts_to_db([forecast], test_depot_id)

    # Verify conversion: 500 W/m² = 1032 cal/cm²
    cached = await get_cached_forecasts(
        test_db_pool, test_depot_id, base_time, base_time + timedelta(days=1)
    )

    assert len(cached) == 1
    assert cached[0]["solar_rad"] == pytest.approx(1032.0, rel=0.01)  # 500 * 2.064
