"""Price forecasting module using ARIMA and Prophet models."""

import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
import logging
from dataclasses import dataclass
import json

try:
    from prophet import Prophet
    PROPHET_AVAILABLE = True
except ImportError:
    PROPHET_AVAILABLE = False
    logging.warning("Prophet not available. Install with: pip install prophet")

try:
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.seasonal import seasonal_decompose
    ARIMA_AVAILABLE = True
except ImportError:
    ARIMA_AVAILABLE = False
    logging.warning("Statsmodels not available. Install with: pip install statsmodels")

from .monitoring import get_logger
from .timescale_client import TimescaleClient


@dataclass
class PriceForecast:
    """Price forecast data structure."""
    forecast_time: datetime
    horizon_start: datetime
    horizon_end: datetime
    node_id: str
    market_type: str
    forecast_prices: List[float]
    confidence_intervals: Optional[List[Tuple[float, float]]] = None
    model_version: str = "unknown"
    accuracy_score: Optional[float] = None


class PriceForecaster:
    """Price forecasting using multiple models."""
    
    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.models: Dict[str, Any] = {}
        self.training_data: Dict[str, pd.DataFrame] = {}
        
        # Model configurations
        self.model_configs = {
            "prophet": {
                "enabled": PROPHET_AVAILABLE,
                "params": {
                    "yearly_seasonality": True,
                    "weekly_seasonality": True,
                    "daily_seasonality": True,
                    "seasonality_mode": "multiplicative",
                    "changepoint_prior_scale": 0.05,
                    "seasonality_prior_scale": 10.0
                }
            },
            "arima": {
                "enabled": ARIMA_AVAILABLE,
                "params": {
                    "order": (2, 1, 2),  # (p, d, q)
                    "seasonal_order": (1, 1, 1, 24)  # (P, D, Q, s) for daily seasonality
                }
            }
        }
    
    async def load_historical_data(self, node_id: str, days: int = 90) -> pd.DataFrame:
        """Load historical price data for training."""
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=days)
            
            # Get historical prices from TimescaleDB
            prices = await self.timescale_client.get_latest_prices(
                nodes=[node_id],
                start=start_time,
                end=end_time
            )
            
            if not prices:
                self.logger.warning(f"No historical data found for node {node_id}")
                return pd.DataFrame()
            
            # Convert to DataFrame
            data = []
            for price in prices:
                data.append({
                    "ds": price["time"],  # Prophet expects 'ds' column
                    "y": price.get("lmp_price_mwh", 0.0) / 1000,  # Convert to $/kWh
                    "timestamp": price["time"]
                })
            
            df = pd.DataFrame(data)
            df["ds"] = pd.to_datetime(df["ds"])
            df = df.sort_values("ds").reset_index(drop=True)
            
            # Remove duplicates and handle missing values
            df = df.drop_duplicates(subset=["ds"])
            df = df.fillna(method="ffill").fillna(method="bfill")
            
            self.logger.info(f"Loaded {len(df)} historical price points for {node_id}")
            return df
            
        except Exception as e:
            self.logger.error(f"Error loading historical data for {node_id}: {e}")
            return pd.DataFrame()
    
    async def train_prophet_model(self, node_id: str, training_data: pd.DataFrame) -> Optional[Prophet]:
        """Train Prophet model for price forecasting."""
        if not PROPHET_AVAILABLE or not self.model_configs["prophet"]["enabled"]:
            self.logger.warning("Prophet not available, skipping Prophet model training")
            return None
        
        try:
            if len(training_data) < 48:  # Need at least 2 days of hourly data
                self.logger.warning(f"Insufficient data for Prophet training: {len(training_data)} points")
                return None
            
            # Initialize Prophet model
            model = Prophet(**self.model_configs["prophet"]["params"])
            
            # Add additional regressors if available
            if "temperature" in training_data.columns:
                model.add_regressor("temperature")
            
            # Train the model
            model.fit(training_data)
            
            self.logger.info(f"Prophet model trained successfully for {node_id}")
            return model
            
        except Exception as e:
            self.logger.error(f"Error training Prophet model for {node_id}: {e}")
            return None
    
    async def train_arima_model(self, node_id: str, training_data: pd.DataFrame) -> Optional[ARIMA]:
        """Train ARIMA model for price forecasting."""
        if not ARIMA_AVAILABLE or not self.model_configs["arima"]["enabled"]:
            self.logger.warning("ARIMA not available, skipping ARIMA model training")
            return None
        
        try:
            if len(training_data) < 100:  # Need sufficient data for ARIMA
                self.logger.warning(f"Insufficient data for ARIMA training: {len(training_data)} points")
                return None
            
            # Prepare time series data
            ts_data = training_data.set_index("ds")["y"]
            
            # Check for stationarity and apply differencing if needed
            from statsmodels.tsa.stattools import adfuller
            adf_result = adfuller(ts_data.dropna())
            
            if adf_result[1] > 0.05:  # Not stationary
                self.logger.info("Data not stationary, applying differencing")
                ts_data = ts_data.diff().dropna()
            
            # Train ARIMA model
            model = ARIMA(ts_data, **self.model_configs["arima"]["params"])
            fitted_model = model.fit()
            
            self.logger.info(f"ARIMA model trained successfully for {node_id}")
            return fitted_model
            
        except Exception as e:
            self.logger.error(f"Error training ARIMA model for {node_id}: {e}")
            return None
    
    async def forecast_with_prophet(self, model: Prophet, horizon_hours: int = 24) -> Tuple[List[float], List[Tuple[float, float]]]:
        """Generate forecast using Prophet model."""
        try:
            # Create future dataframe
            future = model.make_future_dataframe(periods=horizon_hours, freq="H")
            
            # Generate forecast
            forecast = model.predict(future)
            
            # Extract forecast values (last horizon_hours points)
            forecast_values = forecast["yhat"].tail(horizon_hours).tolist()
            
            # Extract confidence intervals
            lower_bounds = forecast["yhat_lower"].tail(horizon_hours).tolist()
            upper_bounds = forecast["yhat_upper"].tail(horizon_hours).tolist()
            confidence_intervals = list(zip(lower_bounds, upper_bounds))
            
            return forecast_values, confidence_intervals
            
        except Exception as e:
            self.logger.error(f"Error generating Prophet forecast: {e}")
            return [], []
    
    async def forecast_with_arima(self, model: ARIMA, horizon_hours: int = 24) -> Tuple[List[float], List[Tuple[float, float]]]:
        """Generate forecast using ARIMA model."""
        try:
            # Generate forecast
            forecast_result = model.forecast(steps=horizon_hours)
            forecast_values = forecast_result.tolist()
            
            # Generate confidence intervals
            conf_int = model.get_forecast(steps=horizon_hours).conf_int()
            confidence_intervals = [(row[0], row[1]) for _, row in conf_int.iterrows()]
            
            return forecast_values, confidence_intervals
            
        except Exception as e:
            self.logger.error(f"Error generating ARIMA forecast: {e}")
            return [], []
    
    async def generate_forecast(
        self, 
        node_id: str, 
        horizon_hours: int = 24,
        model_type: str = "auto"
    ) -> Optional[PriceForecast]:
        """Generate price forecast for a specific node."""
        try:
            # Load historical data
            training_data = await self.load_historical_data(node_id)
            if training_data.empty:
                return None
            
            forecast_time = datetime.now(timezone.utc)
            horizon_start = forecast_time
            horizon_end = forecast_time + timedelta(hours=horizon_hours)
            
            forecast_values = []
            confidence_intervals = []
            model_version = "none"
            
            # Try Prophet first if available and requested
            if model_type in ["auto", "prophet"] and PROPHET_AVAILABLE:
                prophet_model = await self.train_prophet_model(node_id, training_data)
                if prophet_model:
                    forecast_values, confidence_intervals = await self.forecast_with_prophet(
                        prophet_model, horizon_hours
                    )
                    model_version = "prophet"
            
            # Fallback to ARIMA if Prophet failed or not available
            if not forecast_values and model_type in ["auto", "arima"] and ARIMA_AVAILABLE:
                arima_model = await self.train_arima_model(node_id, training_data)
                if arima_model:
                    forecast_values, confidence_intervals = await self.forecast_with_arima(
                        arima_model, horizon_hours
                    )
                    model_version = "arima"
            
            # Simple fallback: use recent average with trend
            if not forecast_values:
                self.logger.warning("All models failed, using simple fallback")
                recent_prices = training_data["y"].tail(24).tolist()
                avg_price = np.mean(recent_prices)
                trend = np.polyfit(range(len(recent_prices)), recent_prices, 1)[0]
                
                forecast_values = [avg_price + trend * i for i in range(horizon_hours)]
                confidence_intervals = [(p * 0.8, p * 1.2) for p in forecast_values]
                model_version = "fallback"
            
            # Calculate accuracy score (if we have recent actual data)
            accuracy_score = await self._calculate_accuracy_score(node_id, forecast_values)
            
            # Create forecast object
            forecast = PriceForecast(
                forecast_time=forecast_time,
                horizon_start=horizon_start,
                horizon_end=horizon_end,
                node_id=node_id,
                market_type="DA",  # Day-ahead market
                forecast_prices=forecast_values,
                confidence_intervals=confidence_intervals,
                model_version=model_version,
                accuracy_score=accuracy_score
            )
            
            # Store forecast in database
            await self._store_forecast(forecast)
            
            self.logger.info(f"Generated {horizon_hours}h forecast for {node_id} using {model_version}")
            return forecast
            
        except Exception as e:
            self.logger.error(f"Error generating forecast for {node_id}: {e}")
            return None
    
    async def _calculate_accuracy_score(self, node_id: str, forecast_values: List[float]) -> Optional[float]:
        """Calculate accuracy score by comparing with recent actual prices."""
        try:
            # Get recent actual prices
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=len(forecast_values))
            
            actual_prices = await self.timescale_client.get_latest_prices(
                nodes=[node_id],
                start=start_time,
                end=end_time
            )
            
            if len(actual_prices) < len(forecast_values):
                return None
            
            # Calculate MAPE (Mean Absolute Percentage Error)
            actual_values = [p.get("lmp_price_mwh", 0.0) / 1000 for p in actual_prices[-len(forecast_values):]]
            
            mape = np.mean([abs(f - a) / a * 100 for f, a in zip(forecast_values, actual_values) if a != 0])
            accuracy_score = max(0, 100 - mape)  # Convert to accuracy percentage
            
            return accuracy_score
            
        except Exception as e:
            self.logger.error(f"Error calculating accuracy score: {e}")
            return None
    
    async def _store_forecast(self, forecast: PriceForecast):
        """Store forecast in TimescaleDB."""
        try:
            forecast_data = {
                "forecast_id": f"forecast_{int(forecast.forecast_time.timestamp())}_{forecast.node_id}",
                "forecast_time": forecast.forecast_time,
                "horizon_start": forecast.horizon_start,
                "horizon_end": forecast.horizon_end,
                "node_id": forecast.node_id,
                "market_type": forecast.market_type,
                "forecast_prices": forecast.forecast_prices,
                "confidence_intervals": forecast.confidence_intervals,
                "model_version": forecast.model_version,
                "accuracy_score": forecast.accuracy_score,
                "created_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_price_forecast(forecast_data)
            
        except Exception as e:
            self.logger.error(f"Error storing forecast: {e}")
    
    async def get_latest_forecast(self, node_id: str) -> Optional[PriceForecast]:
        """Get the latest forecast for a node."""
        try:
            # This would query the database for the most recent forecast
            # For now, we'll generate a new forecast
            return await self.generate_forecast(node_id)
            
        except Exception as e:
            self.logger.error(f"Error getting latest forecast for {node_id}: {e}")
            return None
    
    async def batch_forecast(self, node_ids: List[str], horizon_hours: int = 24) -> Dict[str, PriceForecast]:
        """Generate forecasts for multiple nodes."""
        forecasts = {}
        
        tasks = []
        for node_id in node_ids:
            task = self.generate_forecast(node_id, horizon_hours)
            tasks.append((node_id, task))
        
        # Execute forecasts in parallel
        for node_id, task in tasks:
            try:
                forecast = await task
                if forecast:
                    forecasts[node_id] = forecast
            except Exception as e:
                self.logger.error(f"Error in batch forecast for {node_id}: {e}")
        
        self.logger.info(f"Generated forecasts for {len(forecasts)}/{len(node_ids)} nodes")
        return forecasts
    
    async def retrain_models(self, node_ids: List[str]):
        """Retrain models for specified nodes."""
        for node_id in node_ids:
            try:
                # Load fresh training data
                training_data = await self.load_historical_data(node_id, days=90)
                if training_data.empty:
                    continue
                
                # Retrain models
                if PROPHET_AVAILABLE:
                    prophet_model = await self.train_prophet_model(node_id, training_data)
                    if prophet_model:
                        self.models[f"{node_id}_prophet"] = prophet_model
                
                if ARIMA_AVAILABLE:
                    arima_model = await self.train_arima_model(node_id, training_data)
                    if arima_model:
                        self.models[f"{node_id}_arima"] = arima_model
                
                self.logger.info(f"Retrained models for {node_id}")
                
            except Exception as e:
                self.logger.error(f"Error retraining models for {node_id}: {e}")


class PriceForecastScheduler:
    """Scheduler for automatic price forecasting."""
    
    def __init__(self, forecaster: PriceForecaster):
        self.forecaster = forecaster
        self.logger = get_logger(__name__)
        self.running = False
        self.task: Optional[asyncio.Task] = None
    
    async def start(self, interval_hours: int = 6):
        """Start the forecast scheduler."""
        if self.running:
            return
        
        self.running = True
        self.task = asyncio.create_task(self._scheduler_loop(interval_hours))
        self.logger.info(f"Price forecast scheduler started (interval: {interval_hours}h)")
    
    async def stop(self):
        """Stop the forecast scheduler."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.logger.info("Price forecast scheduler stopped")
    
    async def _scheduler_loop(self, interval_hours: int):
        """Main scheduler loop."""
        while self.running:
            try:
                # Generate forecasts for CAISO nodes
                node_ids = ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"]
                forecasts = await self.forecaster.batch_forecast(node_ids)
                
                self.logger.info(f"Scheduled forecast generation completed: {len(forecasts)} forecasts")
                
            except Exception as e:
                self.logger.error(f"Error in forecast scheduler: {e}")
            
            # Wait for next interval
            await asyncio.sleep(interval_hours * 3600)


# Convenience functions
async def create_price_forecaster(timescale_client: TimescaleClient) -> PriceForecaster:
    """Create and initialize price forecaster."""
    return PriceForecaster(timescale_client)


async def generate_daily_forecasts(timescale_client: TimescaleClient, node_ids: List[str]) -> Dict[str, PriceForecast]:
    """Generate daily price forecasts for specified nodes."""
    forecaster = PriceForecaster(timescale_client)
    return await forecaster.batch_forecast(node_ids, horizon_hours=24)
