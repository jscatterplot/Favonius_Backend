"""Gaussian Process surrogate model for energy consumption prediction.

Reference: PRD.md#8-4-surrogate-model-specification
Following Stanford CarbonFree paper approach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import (
    RBF,
    ConstantKernel,
    WhiteKernel,
)
from sklearn.metrics import r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class PredictionInput:
    """Input features for energy consumption prediction.

    Attributes:
        bus_size: Bus size category ('large' or 'small')
        route_id: Route identifier
        temp_avg_f: Average temperature in Fahrenheit
        temp_max_f: Maximum temperature in Fahrenheit
        temp_min_f: Minimum temperature in Fahrenheit
        rain_inches: Daily rainfall in inches
        solar_radiation: Solar radiation in cal/cm²
        is_school_day: Whether it's a school day
    """

    bus_size: str  # 'large' or 'small'
    route_id: str
    temp_avg_f: float
    temp_max_f: float
    temp_min_f: float
    rain_inches: float
    solar_radiation: float  # cal/cm²
    is_school_day: bool


def compute_degree_days(temp_avg_f: float, base_temp: float = 65.0) -> tuple[float, float]:
    """Compute Heating and Cooling Degree Days.

    Args:
        temp_avg_f: Average temperature in Fahrenheit
        base_temp: Base temperature for degree day calculation (default: 65.0°F)

    Returns:
        Tuple of (heating_degree_days, cooling_degree_days)
    """
    hdd = max(0.0, base_temp - temp_avg_f)
    cdd = max(0.0, temp_avg_f - base_temp)
    return (hdd, cdd)


class EnergySurrogateModel:
    """Gaussian Process model for predicting EV energy consumption.

    This model predicts energy consumption for EV routes based on weather,
    route characteristics, and calendar features. Uses a Gaussian Process
    regressor following the Stanford CarbonFree paper approach.

    Reference: PRD Section 8.4, Development plan Step 2.1
    """

    def __init__(self, known_routes: list[str]):
        """Initialize model with known route IDs.

        Args:
            known_routes: List of route identifiers that will be used
        """
        self.known_routes = known_routes

        # Preprocessing pipeline
        categorical_features = ["bus_size", "route_id"]
        numerical_features = [
            "temp_avg_f",
            "temp_max_f",
            "temp_min_f",
            "rain_inches",
            "solar_radiation",
            "hdd",
            "cdd",
            "is_school_day",
        ]

        self.preprocessor = ColumnTransformer(
            transformers=[
                (
                    "cat",
                    OneHotEncoder(handle_unknown="ignore"),
                    categorical_features,
                ),
                ("num", StandardScaler(), numerical_features),
            ]
        )

        # GP kernel (following Stanford approach)
        kernel = ConstantKernel(1.0, (1e-3, 1e3)) * RBF(
            length_scale=1.0, length_scale_bounds=(1e-2, 1e2)
        ) + WhiteKernel(noise_level=0.1, noise_level_bounds=(1e-5, 1e1))

        self.gp = GaussianProcessRegressor(
            kernel=kernel,
            n_restarts_optimizer=5,
            normalize_y=True,
            random_state=42,
        )

        self.pipeline = Pipeline(
            [
                ("preprocess", self.preprocessor),
                ("gp", self.gp),
            ]
        )

        self._is_fitted = False

        logger.info(f"Initialized EnergySurrogateModel with {len(known_routes)} known routes")

    def _prepare_features(self, inputs: list[PredictionInput]) -> pd.DataFrame:
        """Convert PredictionInput list to feature matrix.

        Args:
            inputs: List of PredictionInput instances

        Returns:
            DataFrame with all features including computed HDD/CDD
        """
        records = []
        for inp in inputs:
            hdd, cdd = compute_degree_days(inp.temp_avg_f)
            records.append(
                {
                    "bus_size": inp.bus_size,
                    "route_id": inp.route_id,
                    "temp_avg_f": inp.temp_avg_f,
                    "temp_max_f": inp.temp_max_f,
                    "temp_min_f": inp.temp_min_f,
                    "rain_inches": inp.rain_inches,
                    "solar_radiation": inp.solar_radiation,
                    "hdd": hdd,
                    "cdd": cdd,
                    "is_school_day": int(inp.is_school_day),
                }
            )

        return pd.DataFrame(records)

    def fit(self, inputs: list[PredictionInput], energy_kwh: list[float]) -> None:
        """Train the surrogate model on historical data.

        Args:
            inputs: List of PredictionInput instances
            energy_kwh: List of actual energy consumption values (kWh)

        Raises:
            ValueError: If inputs and energy_kwh have different lengths or are empty
        """
        if not inputs:
            raise ValueError("inputs cannot be empty")
        if not energy_kwh:
            raise ValueError("energy_kwh cannot be empty")
        if len(inputs) != len(energy_kwh):
            raise ValueError(
                f"inputs length ({len(inputs)}) must match "
                f"energy_kwh length ({len(energy_kwh)})"
            )

        logger.info(f"Fitting model on {len(inputs)} training samples")

        X = self._prepare_features(inputs)
        y = np.array(energy_kwh)

        self.pipeline.fit(X, y)
        self._is_fitted = True

        logger.info("Model fitting complete")

    def predict(self, inputs: list[PredictionInput]) -> tuple[np.ndarray, np.ndarray]:
        """Predict energy consumption with uncertainty.

        Args:
            inputs: List of PredictionInput instances

        Returns:
            Tuple of (mean, std) where:
                - mean: Predicted energy consumption (kWh) as numpy array
                - std: Standard deviation (uncertainty) as numpy array

        Raises:
            RuntimeError: If model is not fitted
        """
        if not self._is_fitted:
            raise RuntimeError("Model not fitted. Call fit() first.")

        if not inputs:
            return np.array([]), np.array([])

        X = self._prepare_features(inputs)

        # Use pipeline for prediction, but need to access GP directly for uncertainty
        # Pipeline doesn't support return_std, so we transform then predict with GP
        X_transformed = self.preprocessor.transform(X)
        mean, std = self.gp.predict(X_transformed, return_std=True)

        logger.debug(f"Predicted energy for {len(inputs)} inputs")

        return mean, std

    def get_r2_score(self, inputs: list[PredictionInput], y_true: list[float]) -> float:
        """Compute R² score on validation data.

        Args:
            inputs: List of PredictionInput instances
            y_true: True energy consumption values (kWh)

        Returns:
            R² score (float)
        """
        y_pred, _ = self.predict(inputs)
        return r2_score(y_true, y_pred)

    def save(self, path: Path) -> None:
        """Save model to disk.

        Args:
            path: Path to save the model file
        """
        import joblib

        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "pipeline": self.pipeline,
            "routes": self.known_routes,
        }

        joblib.dump(data, path)
        logger.info(f"Model saved to {path}")

    @classmethod
    def load(cls, path: Path) -> "EnergySurrogateModel":
        """Load model from disk.

        Args:
            path: Path to the saved model file

        Returns:
            Loaded EnergySurrogateModel instance

        Raises:
            FileNotFoundError: If model file doesn't exist
        """
        import joblib

        if not path.exists():
            raise FileNotFoundError(f"Model file not found: {path}")

        data = joblib.load(path)
        model = cls(data["routes"])
        model.pipeline = data["pipeline"]
        # Restore fitted components from the loaded pipeline
        model.preprocessor = model.pipeline.named_steps["preprocess"]
        model.gp = model.pipeline.named_steps["gp"]
        model._is_fitted = True

        logger.info(f"Model loaded from {path}")

        return model
