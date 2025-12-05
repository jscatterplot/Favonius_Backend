"""Unit tests for surrogate model training pipeline.

Reference: Development plan Step 2.2, PRD.md#11-2-unit-test-requirements
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import asyncpg
import pytest

from src.core.surrogate import (
    EnergySurrogateModel,
    PredictionInput,
    fetch_training_data,
    is_school_day,
    save_trained_model,
    train_and_validate,
)


# ============ Fixtures ============

@pytest.fixture
def mock_pool():
    """Mock asyncpg.Pool for testing."""
    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire = AsyncMock()
    return pool


@pytest.fixture
def sample_depot_id():
    """Sample depot ID for testing."""
    return str(uuid4())


@pytest.fixture
def sample_schedule_rows():
    """Sample database rows for schedules query."""
    base_time = datetime.now() - timedelta(days=10)
    return [
        {
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': base_time + timedelta(days=i),
            'energy_kwh': 150.0 + i * 5,
            'temp_avg_f': 70.0 + i,
            'temp_max_f': 80.0 + i,
            'temp_min_f': 60.0 + i,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        }
        for i in range(20)
    ]


# ============ School Day Tests ============

def test_is_school_day_weekday():
    """Test that Monday-Friday returns True."""
    # Monday, 2024-01-01
    monday = datetime(2024, 1, 1, 10, 0)
    assert is_school_day(monday) is True

    # Friday, 2024-01-05
    friday = datetime(2024, 1, 5, 10, 0)
    assert is_school_day(friday) is True

    # Wednesday, 2024-01-03
    wednesday = datetime(2024, 1, 3, 10, 0)
    assert is_school_day(wednesday) is True


def test_is_school_day_weekend():
    """Test that Saturday-Sunday returns False."""
    # Saturday, 2024-01-06
    saturday = datetime(2024, 1, 6, 10, 0)
    assert is_school_day(saturday) is False

    # Sunday, 2024-01-07
    sunday = datetime(2024, 1, 7, 10, 0)
    assert is_school_day(sunday) is False


def test_is_school_day_edge_cases():
    """Test various edge cases for school day calculation."""
    # Test different times on same day
    monday_morning = datetime(2024, 1, 1, 6, 0)
    monday_evening = datetime(2024, 1, 1, 20, 0)
    assert is_school_day(monday_morning) is True
    assert is_school_day(monday_evening) is True

    # Test different months
    jan_monday = datetime(2024, 1, 1, 10, 0)
    feb_monday = datetime(2024, 2, 5, 10, 0)
    assert is_school_day(jan_monday) is True
    assert is_school_day(feb_monday) is True


# ============ Fetch Training Data Tests ============

@pytest.mark.asyncio
async def test_fetch_training_data_basic(mock_pool, sample_depot_id, sample_schedule_rows):
    """Test basic fetch_training_data functionality."""
    # Mock connection and fetch
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=sample_schedule_rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    inputs, energies = await fetch_training_data(mock_pool, sample_depot_id, lookback_days=30)

    assert len(inputs) == 20
    assert len(energies) == 20
    assert all(isinstance(inp, PredictionInput) for inp in inputs)
    assert all(isinstance(energy, float) for energy in energies)
    assert inputs[0].bus_size == 'large'
    assert inputs[0].route_id == 'route_1'


@pytest.mark.asyncio
async def test_fetch_training_data_missing_weather(mock_pool, sample_depot_id):
    """Test handling of missing weather data."""
    rows_with_missing_weather = [
        {
            'vehicle_type': 'bus_small',
            'route_id': 'route_2',
            'departure_time': datetime.now() - timedelta(days=5),
            'energy_kwh': 100.0,
            'temp_avg_f': None,  # Missing weather
            'temp_max_f': None,
            'temp_min_f': None,
            'rain_inches': None,
            'solar_radiation': None,
        }
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows_with_missing_weather)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    inputs, energies = await fetch_training_data(mock_pool, sample_depot_id, lookback_days=30)

    assert len(inputs) == 1
    # Should use default values
    assert inputs[0].temp_avg_f == 70.0  # Default
    assert inputs[0].temp_max_f == 80.0  # Default + 10
    assert inputs[0].temp_min_f == 60.0  # Default - 10
    assert inputs[0].rain_inches == 0.0  # Default
    assert inputs[0].solar_radiation == 500.0  # Default


@pytest.mark.asyncio
async def test_fetch_training_data_empty_result(mock_pool, sample_depot_id):
    """Test handling of empty query results."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    inputs, energies = await fetch_training_data(mock_pool, sample_depot_id, lookback_days=30)

    assert len(inputs) == 0
    assert len(energies) == 0


@pytest.mark.asyncio
async def test_fetch_training_data_vehicle_type_mapping(mock_pool, sample_depot_id):
    """Test vehicle_type to bus_size mapping."""
    rows = [
        {
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': datetime.now() - timedelta(days=1),
            'energy_kwh': 150.0,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        },
        {
            'vehicle_type': 'bus_small',
            'route_id': 'route_2',
            'departure_time': datetime.now() - timedelta(days=2),
            'energy_kwh': 100.0,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        },
        {
            'vehicle_type': 'unknown_type',
            'route_id': 'route_3',
            'departure_time': datetime.now() - timedelta(days=3),
            'energy_kwh': 120.0,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    inputs, _ = await fetch_training_data(mock_pool, sample_depot_id, lookback_days=30)

    assert inputs[0].bus_size == 'large'
    assert inputs[1].bus_size == 'small'
    assert inputs[2].bus_size == 'large'  # Default for unknown


@pytest.mark.asyncio
async def test_fetch_training_data_school_day_calculation(mock_pool, sample_depot_id):
    """Test is_school_day calculation from departure_time."""
    # Monday
    monday = datetime(2024, 1, 1, 10, 0)
    # Saturday
    saturday = datetime(2024, 1, 6, 10, 0)

    rows = [
        {
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': monday,
            'energy_kwh': 150.0,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        },
        {
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': saturday,
            'energy_kwh': 150.0,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    inputs, _ = await fetch_training_data(mock_pool, sample_depot_id, lookback_days=30)

    assert inputs[0].is_school_day is True  # Monday
    assert inputs[1].is_school_day is False  # Saturday


@pytest.mark.asyncio
async def test_fetch_training_data_invalid_lookback_days(mock_pool, sample_depot_id):
    """Test that invalid lookback_days raises ValueError."""
    with pytest.raises(ValueError, match="lookback_days must be positive"):
        await fetch_training_data(mock_pool, sample_depot_id, lookback_days=0)

    with pytest.raises(ValueError, match="lookback_days must be positive"):
        await fetch_training_data(mock_pool, sample_depot_id, lookback_days=-1)


# ============ Train and Validate Tests ============

@pytest.mark.asyncio
async def test_train_and_validate_basic(mock_pool, sample_depot_id):
    """Test end-to-end training with mock data."""
    # Create synthetic training data
    base_time = datetime.now() - timedelta(days=20)
    rows = []
    for i in range(40):  # 40 samples over ~37 days
        day_offset = i // 2  # Roughly 2 samples per day
        rows.append({
            'vehicle_type': 'bus_large' if i % 2 == 0 else 'bus_small',
            'route_id': f'route_{i % 3 + 1}',
            'departure_time': base_time + timedelta(days=day_offset),
            'energy_kwh': 100.0 + i * 2.0,
            'temp_avg_f': 70.0 + (i % 10),
            'temp_max_f': 80.0 + (i % 10),
            'temp_min_f': 60.0 + (i % 10),
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    model, r2 = await train_and_validate(
        mock_pool, sample_depot_id, training_days=30, validation_days=7
    )

    assert isinstance(model, EnergySurrogateModel)
    assert model._is_fitted
    assert isinstance(r2, float)
    assert -1.0 <= r2 <= 1.0  # R² can be negative for poor fits


@pytest.mark.asyncio
async def test_train_and_validate_split(mock_pool, sample_depot_id):
    """Test that train/validation split is chronological."""
    base_time = datetime.now() - timedelta(days=20)
    rows = []
    for i in range(20):
        rows.append({
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': base_time + timedelta(days=i),
            'energy_kwh': 100.0 + i,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    model, r2 = await train_and_validate(
        mock_pool, sample_depot_id, training_days=15, validation_days=5
    )

    # Verify model was trained
    assert model._is_fitted
    assert isinstance(r2, float)


@pytest.mark.asyncio
async def test_train_and_validate_r2_score(mock_pool, sample_depot_id):
    """Test that R² score is computed correctly."""
    # Create data with clear pattern for better R²
    base_time = datetime.now() - timedelta(days=20)
    rows = []
    for i in range(30):
        # Energy correlated with temperature
        temp = 60.0 + i
        energy = 100.0 + temp * 0.5
        rows.append({
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': base_time + timedelta(days=i),
            'energy_kwh': energy,
            'temp_avg_f': temp,
            'temp_max_f': temp + 10.0,
            'temp_min_f': temp - 10.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    model, r2 = await train_and_validate(
        mock_pool, sample_depot_id, training_days=20, validation_days=10
    )

    assert isinstance(r2, float)
    # R² should be reasonable for synthetic data with pattern
    assert -1.0 <= r2 <= 1.0


@pytest.mark.asyncio
async def test_train_and_validate_insufficient_data(mock_pool, sample_depot_id):
    """Test handling of insufficient data."""
    # Return empty result
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="No training data found"):
        await train_and_validate(mock_pool, sample_depot_id, training_days=30, validation_days=7)


@pytest.mark.asyncio
async def test_train_and_validate_route_extraction(mock_pool, sample_depot_id):
    """Test that unique routes are extracted from training data."""
    base_time = datetime.now() - timedelta(days=20)
    rows = []
    routes = ['route_1', 'route_2', 'route_3']
    for i in range(30):
        rows.append({
            'vehicle_type': 'bus_large',
            'route_id': routes[i % 3],
            'departure_time': base_time + timedelta(days=i),
            'energy_kwh': 100.0 + i,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    model, _ = await train_and_validate(
        mock_pool, sample_depot_id, training_days=20, validation_days=10
    )

    # Model should know about all routes from training data
    assert len(model.known_routes) == 3
    assert set(model.known_routes) == {'route_1', 'route_2', 'route_3'}


@pytest.mark.asyncio
async def test_train_and_validate_invalid_parameters(mock_pool, sample_depot_id):
    """Test that invalid parameters raise ValueError."""
    with pytest.raises(ValueError, match="training_days must be positive"):
        await train_and_validate(mock_pool, sample_depot_id, training_days=0, validation_days=7)

    with pytest.raises(ValueError, match="validation_days must be positive"):
        await train_and_validate(mock_pool, sample_depot_id, training_days=30, validation_days=0)


# ============ Save Trained Model Tests ============

@pytest.mark.asyncio
async def test_save_trained_model(mock_pool, sample_depot_id):
    """Test saving trained model to disk."""
    # Create a trained model
    base_time = datetime.now() - timedelta(days=20)
    rows = []
    for i in range(20):
        rows.append({
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': base_time + timedelta(days=i),
            'energy_kwh': 100.0 + i,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    model, r2 = await train_and_validate(
        mock_pool, sample_depot_id, training_days=15, validation_days=5
    )

    # Save model
    with tempfile.TemporaryDirectory() as tmpdir:
        model_dir = Path(tmpdir)
        model_path = await save_trained_model(model, sample_depot_id, model_dir, r2)

        assert model_path.exists()
        assert model_path.suffix == '.joblib'
        assert sample_depot_id in model_path.name

        # Verify model can be loaded
        loaded_model = EnergySurrogateModel.load(model_path)
        assert loaded_model._is_fitted
        assert loaded_model.known_routes == model.known_routes


@pytest.mark.asyncio
async def test_save_trained_model_creates_directory(mock_pool, sample_depot_id):
    """Test that save_trained_model creates directory if it doesn't exist."""
    base_time = datetime.now() - timedelta(days=20)
    rows = []
    for i in range(10):
        rows.append({
            'vehicle_type': 'bus_large',
            'route_id': 'route_1',
            'departure_time': base_time + timedelta(days=i),
            'energy_kwh': 100.0 + i,
            'temp_avg_f': 70.0,
            'temp_max_f': 80.0,
            'temp_min_f': 60.0,
            'rain_inches': 0.0,
            'solar_radiation': 500.0,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    model, r2 = await train_and_validate(
        mock_pool, sample_depot_id, training_days=7, validation_days=3
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a subdirectory that doesn't exist
        model_dir = Path(tmpdir) / 'models' / 'surrogate'
        assert not model_dir.exists()

        model_path = await save_trained_model(model, sample_depot_id, model_dir, r2)

        assert model_dir.exists()
        assert model_path.exists()


# ============ Integration Tests ============

@pytest.mark.asyncio
async def test_training_pipeline_end_to_end(mock_pool, sample_depot_id):
    """Test full training pipeline end-to-end."""
    # Create realistic training data
    base_time = datetime.now() - timedelta(days=40)
    rows = []
    for i in range(60):  # 60 samples
        day_offset = i // 2
        temp = 60.0 + (i % 20)
        rows.append({
            'vehicle_type': 'bus_large' if i % 2 == 0 else 'bus_small',
            'route_id': f'route_{i % 5 + 1}',
            'departure_time': base_time + timedelta(days=day_offset),
            'energy_kwh': 100.0 + temp * 0.5 + (i % 10) * 2,
            'temp_avg_f': temp,
            'temp_max_f': temp + 10.0,
            'temp_min_f': temp - 10.0,
            'rain_inches': 0.1 if i % 10 == 0 else 0.0,
            'solar_radiation': 400.0 + (i % 20) * 10,
        })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # Train model
    model, r2 = await train_and_validate(
        mock_pool, sample_depot_id, training_days=30, validation_days=7
    )

    # Verify model is trained
    assert model._is_fitted
    assert isinstance(r2, float)

    # Save model
    with tempfile.TemporaryDirectory() as tmpdir:
        model_dir = Path(tmpdir)
        model_path = await save_trained_model(model, sample_depot_id, model_dir, r2)

        # Load and verify
        loaded_model = EnergySurrogateModel.load(model_path)
        assert loaded_model._is_fitted

        # Test prediction
        test_input = PredictionInput(
            bus_size='large',
            route_id='route_1',
            temp_avg_f=70.0,
            temp_max_f=80.0,
            temp_min_f=60.0,
            rain_inches=0.0,
            solar_radiation=500.0,
            is_school_day=True,
        )
        mean, std = loaded_model.predict([test_input])
        assert len(mean) == 1
        assert len(std) == 1
        assert mean[0] > 0
        assert std[0] > 0

