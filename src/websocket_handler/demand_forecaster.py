"""Demand forecasting module using LSTM and XGBoost models."""

import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
import logging
from dataclasses import dataclass
import json

try:
    import xgboost as xgb
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False
    logging.warning("XGBoost not available. Install with: pip install xgboost")

try:
    import tensorflow as tf
    from tensorflow.keras.models import Sequential
    from tensorflow.keras.layers import LSTM, Dense, Dropout
    from tensorflow.keras.optimizers import Adam
    LSTM_AVAILABLE = True
except ImportError:
    LSTM_AVAILABLE = False
    logging.warning("TensorFlow not available. Install with: pip install tensorflow")

from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error

from .monitoring import get_logger
from .timescale_client import TimescaleClient


@dataclass
class DemandForecast:
    """Demand forecast data structure."""
    forecast_time: datetime
    horizon_start: datetime
    horizon_end: datetime
    station_id: str
    forecast_demand: List[float]
    confidence_intervals: Optional[List[Tuple[float, float]]] = None
    model_version: str = "unknown"
    accuracy_score: Optional[float] = None


class DemandForecaster:
    """Demand forecasting using LSTM and XGBoost models."""
    
    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.models: Dict[str, Any] = {}
        self.scalers: Dict[str, MinMaxScaler] = {}
        
        # Model configurations
        self.model_configs = {
            "lstm": {
                "enabled": LSTM_AVAILABLE,
                "params": {
                    "sequence_length": 24,  # 24 hours of historical data
                    "lstm_units": 50,
                    "dropout_rate": 0.2,
                    "epochs": 100,
                    "batch_size": 32,
                    "learning_rate": 0.001
                }
            },
            "xgboost": {
                "enabled": XGBOOST_AVAILABLE,
                "params": {
                    "n_estimators": 100,
                    "max_depth": 6,
                    "learning_rate": 0.1,
                    "subsample": 0.8,
                    "colsample_bytree": 0.8,
                    "random_state": 42
                }
            }
        }
    
    async def load_historical_data(self, station_id: str, days: int = 30) -> pd.DataFrame:
        """Load historical demand data for training."""
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=days)
            
            # Get historical telemetry data
            telemetry_data = await self.timescale_client.get_telemetry_data(
                station_id=station_id,
                start_time=start_time,
                end_time=end_time
            )
            
            if not telemetry_data:
                self.logger.warning(f"No historical data found for station {station_id}")
                return pd.DataFrame()
            
            # Convert to DataFrame and aggregate by hour
            data = []
            for record in telemetry_data:
                data.append({
                    "timestamp": record["time"],
                    "power_kw": record.get("power_kw", 0.0),
                    "energy_kwh": record.get("energy_kwh", 0.0),
                    "voltage_v": record.get("voltage_v", 0.0),
                    "current_a": record.get("current_a", 0.0),
                    "temperature_c": record.get("temperature_c", 20.0)
                })
            
            df = pd.DataFrame(data)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.set_index("timestamp")
            
            # Resample to hourly data
            hourly_data = df.resample("H").agg({
                "power_kw": "mean",
                "energy_kwh": "sum",
                "voltage_v": "mean",
                "current_a": "mean",
                "temperature_c": "mean"
            }).fillna(method="ffill").fillna(method="bfill")
            
            # Add time features
            hourly_data["hour"] = hourly_data.index.hour
            hourly_data["day_of_week"] = hourly_data.index.dayofweek
            hourly_data["day_of_month"] = hourly_data.index.day
            hourly_data["month"] = hourly_data.index.month
            
            # Calculate demand (power consumption)
            hourly_data["demand_kw"] = hourly_data["power_kw"].abs()
            
            self.logger.info(f"Loaded {len(hourly_data)} hourly data points for {station_id}")
            return hourly_data
            
        except Exception as e:
            self.logger.error(f"Error loading historical data for {station_id}: {e}")
            return pd.DataFrame()
    
    def prepare_lstm_data(self, data: pd.DataFrame, sequence_length: int = 24) -> Tuple[np.ndarray, np.ndarray]:
        """Prepare data for LSTM training."""
        try:
            # Select features for LSTM
            features = ["demand_kw", "temperature_c", "hour", "day_of_week"]
            feature_data = data[features].values
            
            # Normalize the data
            scaler = MinMaxScaler()
            scaled_data = scaler.fit_transform(feature_data)
            
            # Create sequences
            X, y = [], []
            for i in range(sequence_length, len(scaled_data)):
                X.append(scaled_data[i-sequence_length:i])
                y.append(scaled_data[i, 0])  # demand_kw is first feature
            
            return np.array(X), np.array(y), scaler
            
        except Exception as e:
            self.logger.error(f"Error preparing LSTM data: {e}")
            return None, None, None
    
    async def train_lstm_model(self, station_id: str, training_data: pd.DataFrame) -> Optional[Any]:
        """Train LSTM model for demand forecasting."""
        if not LSTM_AVAILABLE or not self.model_configs["lstm"]["enabled"]:
            self.logger.warning("LSTM not available, skipping LSTM model training")
            return None
        
        try:
            if len(training_data) < 168:  # Need at least 1 week of hourly data
                self.logger.warning(f"Insufficient data for LSTM training: {len(training_data)} points")
                return None
            
            sequence_length = self.model_configs["lstm"]["params"]["sequence_length"]
            X, y, scaler = self.prepare_lstm_data(training_data, sequence_length)
            
            if X is None or y is None:
                return None
            
            # Store scaler for later use
            self.scalers[f"{station_id}_lstm"] = scaler
            
            # Split data into train/validation
            split_idx = int(0.8 * len(X))
            X_train, X_val = X[:split_idx], X[split_idx:]
            y_train, y_val = y[:split_idx], y[split_idx:]
            
            # Build LSTM model
            model = Sequential([
                LSTM(self.model_configs["lstm"]["params"]["lstm_units"], 
                     return_sequences=True, 
                     input_shape=(sequence_length, X.shape[2])),
                Dropout(self.model_configs["lstm"]["params"]["dropout_rate"]),
                LSTM(self.model_configs["lstm"]["params"]["lstm_units"], 
                     return_sequences=False),
                Dropout(self.model_configs["lstm"]["params"]["dropout_rate"]),
                Dense(25),
                Dense(1)
            ])
            
            model.compile(
                optimizer=Adam(learning_rate=self.model_configs["lstm"]["params"]["learning_rate"]),
                loss="mse",
                metrics=["mae"]
            )
            
            # Train the model
            history = model.fit(
                X_train, y_train,
                validation_data=(X_val, y_val),
                epochs=self.model_configs["lstm"]["params"]["epochs"],
                batch_size=self.model_configs["lstm"]["params"]["batch_size"],
                verbose=0
            )
            
            self.logger.info(f"LSTM model trained successfully for {station_id}")
            return model
            
        except Exception as e:
            self.logger.error(f"Error training LSTM model for {station_id}: {e}")
            return None
    
    async def train_xgboost_model(self, station_id: str, training_data: pd.DataFrame) -> Optional[Any]:
        """Train XGBoost model for demand forecasting."""
        if not XGBOOST_AVAILABLE or not self.model_configs["xgboost"]["enabled"]:
            self.logger.warning("XGBoost not available, skipping XGBoost model training")
            return None
        
        try:
            if len(training_data) < 168:  # Need at least 1 week of hourly data
                self.logger.warning(f"Insufficient data for XGBoost training: {len(training_data)} points")
                return None
            
            # Prepare features
            feature_columns = ["demand_kw", "temperature_c", "hour", "day_of_week", "day_of_month", "month"]
            
            # Create lagged features
            for lag in [1, 2, 3, 6, 12, 24]:  # 1h, 2h, 3h, 6h, 12h, 24h ago
                training_data[f"demand_lag_{lag}"] = training_data["demand_kw"].shift(lag)
                feature_columns.append(f"demand_lag_{lag}")
            
            # Create rolling statistics
            for window in [3, 6, 12, 24]:  # 3h, 6h, 12h, 24h rolling windows
                training_data[f"demand_rolling_mean_{window}"] = training_data["demand_kw"].rolling(window).mean()
                training_data[f"demand_rolling_std_{window}"] = training_data["demand_kw"].rolling(window).std()
                feature_columns.extend([f"demand_rolling_mean_{window}", f"demand_rolling_std_{window}"])
            
            # Remove rows with NaN values
            training_data_clean = training_data.dropna()
            
            if len(training_data_clean) < 100:
                self.logger.warning(f"Insufficient clean data for XGBoost training: {len(training_data_clean)} points")
                return None
            
            # Prepare features and target
            X = training_data_clean[feature_columns]
            y = training_data_clean["demand_kw"]
            
            # Split data
            split_idx = int(0.8 * len(X))
            X_train, X_val = X[:split_idx], X[split_idx:]
            y_train, y_val = y[:split_idx], y[split_idx:]
            
            # Train XGBoost model
            model = xgb.XGBRegressor(**self.model_configs["xgboost"]["params"])
            model.fit(X_train, y_train, 
                     eval_set=[(X_val, y_val)], 
                     early_stopping_rounds=10, 
                     verbose=False)
            
            self.logger.info(f"XGBoost model trained successfully for {station_id}")
            return model
            
        except Exception as e:
            self.logger.error(f"Error training XGBoost model for {station_id}: {e}")
            return None
    
    async def forecast_with_lstm(self, model: Any, scaler: MinMaxScaler, 
                               last_data: pd.DataFrame, horizon_hours: int = 24) -> Tuple[List[float], List[Tuple[float, float]]]:
        """Generate forecast using LSTM model."""
        try:
            sequence_length = self.model_configs["lstm"]["params"]["sequence_length"]
            features = ["demand_kw", "temperature_c", "hour", "day_of_week"]
            
            # Prepare the last sequence
            last_sequence = last_data[features].tail(sequence_length).values
            scaled_sequence = scaler.transform(last_sequence)
            
            forecast_values = []
            current_sequence = scaled_sequence.copy()
            
            # Generate forecast step by step
            for _ in range(horizon_hours):
                # Reshape for prediction
                X_pred = current_sequence.reshape(1, sequence_length, len(features))
                
                # Predict next value
                next_pred = model.predict(X_pred, verbose=0)[0][0]
                forecast_values.append(next_pred)
                
                # Update sequence (shift and add new prediction)
                current_sequence = np.roll(current_sequence, -1, axis=0)
                current_sequence[-1, 0] = next_pred  # Update demand_kw
                
                # Update time features for next hour
                next_hour = (last_data.index[-1] + timedelta(hours=len(forecast_values))).hour
                next_day_of_week = (last_data.index[-1] + timedelta(hours=len(forecast_values))).dayofweek
                current_sequence[-1, 2] = next_hour / 23.0  # Normalize hour
                current_sequence[-1, 3] = next_day_of_week / 6.0  # Normalize day of week
            
            # Inverse transform the forecast
            forecast_array = np.array(forecast_values).reshape(-1, 1)
            # Create dummy array for inverse transform
            dummy_array = np.zeros((len(forecast_values), len(features)))
            dummy_array[:, 0] = forecast_values
            forecast_values = scaler.inverse_transform(dummy_array)[:, 0]
            
            # Generate confidence intervals (simple approach)
            std_dev = np.std(forecast_values) * 0.1  # 10% of std dev
            confidence_intervals = [(max(0, v - std_dev), v + std_dev) for v in forecast_values]
            
            return forecast_values.tolist(), confidence_intervals
            
        except Exception as e:
            self.logger.error(f"Error generating LSTM forecast: {e}")
            return [], []
    
    async def forecast_with_xgboost(self, model: Any, last_data: pd.DataFrame, 
                                   horizon_hours: int = 24) -> Tuple[List[float], List[Tuple[float, float]]]:
        """Generate forecast using XGBoost model."""
        try:
            forecast_values = []
            
            # Prepare features for prediction
            feature_columns = ["demand_kw", "temperature_c", "hour", "day_of_week", "day_of_month", "month"]
            
            # Create lagged features
            for lag in [1, 2, 3, 6, 12, 24]:
                feature_columns.append(f"demand_lag_{lag}")
            
            # Create rolling statistics
            for window in [3, 6, 12, 24]:
                feature_columns.extend([f"demand_rolling_mean_{window}", f"demand_rolling_std_{window}"])
            
            # Generate forecast step by step
            current_data = last_data.copy()
            
            for i in range(horizon_hours):
                # Prepare features for current timestep
                features_dict = {}
                
                # Basic features
                next_time = current_data.index[-1] + timedelta(hours=1)
                features_dict["demand_kw"] = current_data["demand_kw"].iloc[-1]
                features_dict["temperature_c"] = current_data["temperature_c"].iloc[-1]  # Assume constant temperature
                features_dict["hour"] = next_time.hour
                features_dict["day_of_week"] = next_time.dayofweek
                features_dict["day_of_month"] = next_time.day
                features_dict["month"] = next_time.month
                
                # Lagged features
                for lag in [1, 2, 3, 6, 12, 24]:
                    if len(current_data) > lag:
                        features_dict[f"demand_lag_{lag}"] = current_data["demand_kw"].iloc[-lag]
                    else:
                        features_dict[f"demand_lag_{lag}"] = current_data["demand_kw"].mean()
                
                # Rolling statistics
                for window in [3, 6, 12, 24]:
                    if len(current_data) >= window:
                        features_dict[f"demand_rolling_mean_{window}"] = current_data["demand_kw"].tail(window).mean()
                        features_dict[f"demand_rolling_std_{window}"] = current_data["demand_kw"].tail(window).std()
                    else:
                        features_dict[f"demand_rolling_mean_{window}"] = current_data["demand_kw"].mean()
                        features_dict[f"demand_rolling_std_{window}"] = current_data["demand_kw"].std()
                
                # Create feature vector
                X_pred = np.array([features_dict[col] for col in feature_columns]).reshape(1, -1)
                
                # Predict next value
                next_pred = model.predict(X_pred)[0]
                forecast_values.append(max(0, next_pred))  # Ensure non-negative
                
                # Update current_data with prediction
                new_row = current_data.iloc[-1].copy()
                new_row["demand_kw"] = next_pred
                new_row.name = next_time
                current_data = pd.concat([current_data, new_row.to_frame().T])
            
            # Generate confidence intervals
            std_dev = np.std(forecast_values) * 0.15  # 15% of std dev
            confidence_intervals = [(max(0, v - std_dev), v + std_dev) for v in forecast_values]
            
            return forecast_values, confidence_intervals
            
        except Exception as e:
            self.logger.error(f"Error generating XGBoost forecast: {e}")
            return [], []
    
    async def generate_forecast(
        self, 
        station_id: str, 
        horizon_hours: int = 24,
        model_type: str = "auto"
    ) -> Optional[DemandForecast]:
        """Generate demand forecast for a specific station."""
        try:
            # Load historical data
            training_data = await self.load_historical_data(station_id)
            if training_data.empty:
                return None
            
            forecast_time = datetime.now(timezone.utc)
            horizon_start = forecast_time
            horizon_end = forecast_time + timedelta(hours=horizon_hours)
            
            forecast_values = []
            confidence_intervals = []
            model_version = "none"
            
            # Try LSTM first if available and requested
            if model_type in ["auto", "lstm"] and LSTM_AVAILABLE:
                lstm_model = await self.train_lstm_model(station_id, training_data)
                if lstm_model:
                    scaler = self.scalers.get(f"{station_id}_lstm")
                    if scaler:
                        forecast_values, confidence_intervals = await self.forecast_with_lstm(
                            lstm_model, scaler, training_data, horizon_hours
                        )
                        model_version = "lstm"
            
            # Fallback to XGBoost if LSTM failed or not available
            if not forecast_values and model_type in ["auto", "xgboost"] and XGBOOST_AVAILABLE:
                xgb_model = await self.train_xgboost_model(station_id, training_data)
                if xgb_model:
                    forecast_values, confidence_intervals = await self.forecast_with_xgboost(
                        xgb_model, training_data, horizon_hours
                    )
                    model_version = "xgboost"
            
            # Simple fallback: use recent average with trend
            if not forecast_values:
                self.logger.warning("All models failed, using simple fallback")
                recent_demand = training_data["demand_kw"].tail(24).tolist()
                avg_demand = np.mean(recent_demand)
                trend = np.polyfit(range(len(recent_demand)), recent_demand, 1)[0]
                
                forecast_values = [max(0, avg_demand + trend * i) for i in range(horizon_hours)]
                confidence_intervals = [(max(0, v * 0.8), v * 1.2) for v in forecast_values]
                model_version = "fallback"
            
            # Calculate accuracy score
            accuracy_score = await self._calculate_accuracy_score(station_id, forecast_values)
            
            # Create forecast object
            forecast = DemandForecast(
                forecast_time=forecast_time,
                horizon_start=horizon_start,
                horizon_end=horizon_end,
                station_id=station_id,
                forecast_demand=forecast_values,
                confidence_intervals=confidence_intervals,
                model_version=model_version,
                accuracy_score=accuracy_score
            )
            
            # Store forecast in database
            await self._store_forecast(forecast)
            
            self.logger.info(f"Generated {horizon_hours}h demand forecast for {station_id} using {model_version}")
            return forecast
            
        except Exception as e:
            self.logger.error(f"Error generating demand forecast for {station_id}: {e}")
            return None
    
    async def _calculate_accuracy_score(self, station_id: str, forecast_values: List[float]) -> Optional[float]:
        """Calculate accuracy score by comparing with recent actual demand."""
        try:
            # Get recent actual demand
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=len(forecast_values))
            
            actual_data = await self.timescale_client.get_telemetry_data(
                station_id=station_id,
                start_time=start_time,
                end_time=end_time
            )
            
            if len(actual_data) < len(forecast_values):
                return None
            
            # Aggregate to hourly data
            df = pd.DataFrame(actual_data)
            df["timestamp"] = pd.to_datetime(df["time"])
            df = df.set_index("timestamp")
            hourly_actual = df["power_kw"].resample("H").mean().abs().tail(len(forecast_values))
            
            # Calculate MAPE
            actual_values = hourly_actual.tolist()
            mape = np.mean([abs(f - a) / a * 100 for f, a in zip(forecast_values, actual_values) if a != 0])
            accuracy_score = max(0, 100 - mape)
            
            return accuracy_score
            
        except Exception as e:
            self.logger.error(f"Error calculating accuracy score: {e}")
            return None
    
    async def _store_forecast(self, forecast: DemandForecast):
        """Store forecast in TimescaleDB."""
        try:
            forecast_data = {
                "forecast_id": f"demand_forecast_{int(forecast.forecast_time.timestamp())}_{forecast.station_id}",
                "forecast_time": forecast.forecast_time,
                "horizon_start": forecast.horizon_start,
                "horizon_end": forecast.horizon_end,
                "station_id": forecast.station_id,
                "forecast_demand": forecast.forecast_demand,
                confidence_intervals": forecast.confidence_intervals,
                "model_version": forecast.model_version,
                "accuracy_score": forecast.accuracy_score,
                "created_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_demand_forecast(forecast_data)
            
        except Exception as e:
            self.logger.error(f"Error storing demand forecast: {e}")
    
    async def batch_forecast(self, station_ids: List[str], horizon_hours: int = 24) -> Dict[str, DemandForecast]:
        """Generate forecasts for multiple stations."""
        forecasts = {}
        
        tasks = []
        for station_id in station_ids:
            task = self.generate_forecast(station_id, horizon_hours)
            tasks.append((station_id, task))
        
        # Execute forecasts in parallel
        for station_id, task in tasks:
            try:
                forecast = await task
                if forecast:
                    forecasts[station_id] = forecast
            except Exception as e:
                self.logger.error(f"Error in batch demand forecast for {station_id}: {e}")
        
        self.logger.info(f"Generated demand forecasts for {len(forecasts)}/{len(station_ids)} stations")
        return forecasts


class DemandForecastScheduler:
    """Scheduler for automatic demand forecasting."""
    
    def __init__(self, forecaster: DemandForecaster):
        self.forecaster = forecaster
        self.logger = get_logger(__name__)
        self.running = False
        self.task: Optional[asyncio.Task] = None
    
    async def start(self, interval_hours: int = 4):
        """Start the demand forecast scheduler."""
        if self.running:
            return
        
        self.running = True
        self.task = asyncio.create_task(self._scheduler_loop(interval_hours))
        self.logger.info(f"Demand forecast scheduler started (interval: {interval_hours}h)")
    
    async def stop(self):
        """Stop the demand forecast scheduler."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.logger.info("Demand forecast scheduler stopped")
    
    async def _scheduler_loop(self, interval_hours: int):
        """Main scheduler loop."""
        while self.running:
            try:
                # Generate forecasts for all stations
                station_ids = ["default_station"]  # TODO: Get from configuration
                forecasts = await self.forecaster.batch_forecast(station_ids)
                
                self.logger.info(f"Scheduled demand forecast generation completed: {len(forecasts)} forecasts")
                
            except Exception as e:
                self.logger.error(f"Error in demand forecast scheduler: {e}")
            
            # Wait for next interval
            await asyncio.sleep(interval_hours * 3600)


# Convenience functions
async def create_demand_forecaster(timescale_client: TimescaleClient) -> DemandForecaster:
    """Create and initialize demand forecaster."""
    return DemandForecaster(timescale_client)


async def generate_daily_demand_forecasts(timescale_client: TimescaleClient, station_ids: List[str]) -> Dict[str, DemandForecast]:
    """Generate daily demand forecasts for specified stations."""
    forecaster = DemandForecaster(timescale_client)
    return await forecaster.batch_forecast(station_ids, horizon_hours=24)
