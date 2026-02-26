"""Training pipeline for energy consumption surrogate model.

Reference: Development plan Step 2.2, PRD.md#8-4-surrogate-model-specification
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg

from .energy_model import EnergySurrogateModel, PredictionInput

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def is_school_day(dt: datetime) -> bool:
    """Determine if a given date is a school day.

    For MVP: Simple weekday check (Monday-Friday = school day).
    Future: Add holiday calendar support.

    Args:
        dt: Datetime object to check

    Returns:
        True if the date is a school day (Monday-Friday), False otherwise
    """
    # Monday = 0, Friday = 4
    return dt.weekday() < 5


async def fetch_training_data(
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    lookback_days: int = 30,
) -> tuple[list[PredictionInput], list[float]]:
    """Fetch training data from database.

    Joins schedules, vehicles, and weather_forecasts tables to construct
    PredictionInput objects and corresponding energy consumption values.

    Args:
        pool: AsyncPG connection pool
        depot_id: Depot identifier (UUID string or UUID object)
        lookback_days: Number of days to look back for training data

    Returns:
        Tuple of (list of PredictionInput, list of energy_kwh values)

    Raises:
        ValueError: If lookback_days is not positive
        asyncpg.PostgresError: If database query fails
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be positive, got {lookback_days}")

    # Convert depot_id to string if it's a UUID
    depot_id_str = str(depot_id) if isinstance(depot_id, UUID) else depot_id

    logger.info(
        f"Fetching training data for depot {depot_id_str}, " f"lookback_days={lookback_days}"
    )

    query = """
    SELECT 
        v.vehicle_type,
        s.route_id,
        s.departure_time,
        s.energy_kwh,
        w.temp_f as temp_avg_f,
        w.temp_max_f,
        w.temp_min_f,
        w.precip_in as rain_inches,
        w.solar_rad as solar_radiation
    FROM schedules s
    JOIN vehicles v ON s.vehicle_id = v.vehicle_id
    LEFT JOIN weather_forecasts w ON 
        w.depot_id = v.depot_id AND
        DATE(w.time) = DATE(s.departure_time)
    WHERE v.depot_id = $1::uuid
      AND s.departure_time > NOW() - INTERVAL '%s days'
      AND s.energy_kwh IS NOT NULL
    ORDER BY s.departure_time
    """ % lookback_days

    inputs = []
    energies = []

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, depot_id_str)

        logger.debug(f"Fetched {len(rows)} rows from database")

        # Default weather values if missing
        default_temp = 70.0  # Fahrenheit
        default_rain = 0.0
        default_solar = 500.0  # cal/cm²

        missing_weather_count = 0

        for row in rows:
            # Map vehicle_type to bus_size
            vehicle_type = row["vehicle_type"] or ""
            if "large" in vehicle_type.lower():
                bus_size = "large"
            elif "small" in vehicle_type.lower():
                bus_size = "small"
            else:
                # Default to 'large' if unclear
                bus_size = "large"
                logger.warning(f"Unknown vehicle_type '{vehicle_type}', defaulting to 'large'")

            # Get weather data or use defaults
            temp_avg_f = row["temp_avg_f"] if row["temp_avg_f"] is not None else default_temp
            temp_max_f = row["temp_max_f"] if row["temp_max_f"] is not None else (temp_avg_f + 10.0)
            temp_min_f = row["temp_min_f"] if row["temp_min_f"] is not None else (temp_avg_f - 10.0)
            rain_inches = row["rain_inches"] if row["rain_inches"] is not None else default_rain
            solar_radiation = (
                row["solar_radiation"] if row["solar_radiation"] is not None else default_solar
            )

            if row["temp_avg_f"] is None:
                missing_weather_count += 1

            # Calculate is_school_day from departure_time
            departure_time = row["departure_time"]
            if isinstance(departure_time, str):
                departure_time = datetime.fromisoformat(departure_time.replace("Z", "+00:00"))
            school_day = is_school_day(departure_time)

            # Create PredictionInput
            inputs.append(
                PredictionInput(
                    bus_size=bus_size,
                    route_id=row["route_id"] or "unknown",
                    temp_avg_f=float(temp_avg_f),
                    temp_max_f=float(temp_max_f),
                    temp_min_f=float(temp_min_f),
                    rain_inches=float(rain_inches),
                    solar_radiation=float(solar_radiation),
                    is_school_day=school_day,
                )
            )

            # Energy consumption (already validated as NOT NULL in query)
            energies.append(float(row["energy_kwh"]))

        if missing_weather_count > 0:
            logger.warning(
                f"Missing weather data for {missing_weather_count} out of {len(rows)} records. "
                "Using default values."
            )

        logger.info(f"Fetched {len(inputs)} training samples for depot {depot_id_str}")

    except asyncpg.PostgresError as e:
        logger.error(f"Database error fetching training data: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching training data: {e}")
        raise

    return inputs, energies


async def train_and_validate(
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    training_days: int = 30,
    validation_days: int = 7,
) -> tuple[EnergySurrogateModel, float]:
    """Train model and return R² score on validation set.

    Fetches data for training_days + validation_days period, splits
    chronologically, trains the model, and validates on held-out data.

    Args:
        pool: AsyncPG connection pool
        depot_id: Depot identifier
        training_days: Number of days to use for training
        validation_days: Number of days to use for validation

    Returns:
        Tuple of (trained EnergySurrogateModel, R² score on validation set)

    Raises:
        ValueError: If training_days or validation_days are not positive
        RuntimeError: If insufficient data for training/validation split
    """
    if training_days <= 0:
        raise ValueError(f"training_days must be positive, got {training_days}")
    if validation_days <= 0:
        raise ValueError(f"validation_days must be positive, got {validation_days}")

    logger.info(
        f"Training model for depot {depot_id}, "
        f"training_days={training_days}, validation_days={validation_days}"
    )

    # Fetch data for the full period
    total_days = training_days + validation_days
    all_inputs, all_energies = await fetch_training_data(pool, depot_id, lookback_days=total_days)

    if len(all_inputs) == 0:
        raise RuntimeError(
            f"No training data found for depot {depot_id} in the last {total_days} days"
        )

    # Split chronologically: first N days for training, last M days for validation
    # Estimate samples per day (rough approximation)
    samples_per_day = len(all_inputs) / total_days if total_days > 0 else 1
    split_idx = int(training_days * samples_per_day)

    # Ensure we have at least some validation data
    if split_idx >= len(all_inputs):
        split_idx = max(1, len(all_inputs) - int(validation_days * samples_per_day))

    train_X = all_inputs[:split_idx]
    train_y = all_energies[:split_idx]
    val_X = all_inputs[split_idx:]
    val_y = all_energies[split_idx:]

    if len(train_X) == 0:
        raise RuntimeError(
            f"Insufficient data for training: need at least 1 sample, got {len(train_X)}"
        )
    if len(val_X) == 0:
        raise RuntimeError(
            f"Insufficient data for validation: need at least 1 sample, got {len(val_X)}"
        )

    logger.info(f"Split data: {len(train_X)} training samples, {len(val_X)} validation samples")

    # Extract unique routes from training data only
    routes = list(set(inp.route_id for inp in train_X))
    logger.info(f"Found {len(routes)} unique routes: {routes}")

    # Train model
    model = EnergySurrogateModel(known_routes=routes)
    logger.info("Fitting model on training data...")
    model.fit(train_X, train_y)

    # Validate
    logger.info("Computing R² score on validation set...")
    r2 = model.get_r2_score(val_X, val_y)

    logger.info(
        f"Training complete: R² score on validation set = {r2:.4f} "
        f"(training samples: {len(train_X)}, validation samples: {len(val_X)})"
    )

    return model, r2


async def save_trained_model(
    model: EnergySurrogateModel,
    depot_id: str | UUID,
    model_dir: Path,
    r2_score: float,
) -> Path:
    """Save trained model to disk with metadata.

    Creates model directory if it doesn't exist, generates filename
    with depot_id and timestamp, and saves the model.

    Args:
        model: Trained EnergySurrogateModel instance
        depot_id: Depot identifier
        model_dir: Directory to save model in
        r2_score: R² score from validation (for metadata)

    Returns:
        Path to saved model file

    Raises:
        OSError: If model directory cannot be created
    """
    # Convert depot_id to string
    depot_id_str = str(depot_id) if isinstance(depot_id, UUID) else depot_id

    # Create model directory if it doesn't exist
    model_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with depot_id and timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"surrogate_model_{depot_id_str}_{timestamp}.joblib"
    model_path = model_dir / filename

    logger.info(f"Saving trained model to {model_path}")

    # Save model
    model.save(model_path)

    # Optionally save metadata (could be extended to save JSON file)
    logger.info(f"Model saved: depot={depot_id_str}, R²={r2_score:.4f}, " f"path={model_path}")

    return model_path
