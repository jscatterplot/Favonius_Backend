"""Energy consumption surrogate model (Gaussian Process/MLP).

Reference: PRD.md#8-4-surrogate-model-specification
"""

from .energy_model import (
    EnergySurrogateModel,
    PredictionInput,
    compute_degree_days,
)
from .training import (
    fetch_training_data,
    is_school_day,
    save_trained_model,
    train_and_validate,
)

__all__ = [
    "PredictionInput",
    "compute_degree_days",
    "EnergySurrogateModel",
    "fetch_training_data",
    "is_school_day",
    "save_trained_model",
    "train_and_validate",
]
