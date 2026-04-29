"""Weather API integration (Open-Meteo)."""

from .ingestion import WeatherIngestionService
from .openmeteo import OpenMeteoAdapter, WeatherData
from .storage import (
    DEFAULT_WEATHER_SOURCE,
    convert_solar_radiation_wm2_to_calcm2,
    get_cached_forecasts,
    get_depot_location,
    get_latest_forecast,
    get_latest_forecast_bundle,
    store_weather_forecasts,
)

__all__ = [
    "OpenMeteoAdapter",
    "WeatherData",
    "WeatherIngestionService",
    "store_weather_forecasts",
    "get_cached_forecasts",
    "get_latest_forecast",
    "get_latest_forecast_bundle",
    "get_depot_location",
    "convert_solar_radiation_wm2_to_calcm2",
    "DEFAULT_WEATHER_SOURCE",
]
