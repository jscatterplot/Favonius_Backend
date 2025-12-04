"""Enhanced API server with optimization and forecasting endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
import uuid

from ..timescale_client import TimescaleClient
from ..monitoring import get_logger
from ..price_forecaster import PriceForecaster, PriceForecastScheduler
from ..demand_forecaster import DemandForecaster, DemandForecastScheduler
from ..fleet_connectors import FleetConnectorManager, create_connector
from ..optimization_engine import OptimizationEngine
from ..julia_bridge import JuliaBridge

router = APIRouter()
logger = get_logger(__name__)

# Global instances (would be injected in real application)
price_forecaster: Optional[PriceForecaster] = None
demand_forecaster: Optional[DemandForecaster] = None
fleet_manager: Optional[FleetConnectorManager] = None
optimization_engine: Optional[OptimizationEngine] = None
julia_bridge: Optional[JuliaBridge] = None

# Dependency to get TimescaleClient
async def get_timescale_client() -> TimescaleClient:
    from ..config import Config
    config = Config()
    client = TimescaleClient(config.timescale_config)
    if not client.connected:
        await client.connect()
    return client

# --- Pydantic Models for API Request/Response ---

class OptimizationRequest(BaseModel):
    vehicle_ids: List[str]
    horizon_hours: int = 24
    timestep_minutes: int = 15
    facility_capacity_kw: float = 200.0
    cost_weight: float = 1.0
    peak_weight: float = 0.1
    demand_weight: float = 0.05
    use_forecasts: bool = True

class OptimizationResponse(BaseModel):
    optimization_id: str
    status: str
    objective_value: float
    solve_time_ms: float
    total_cost: float
    peak_demand_kw: float
    charging_schedules: List[Dict[str, Any]]
    created_at: datetime

class ForecastRequest(BaseModel):
    node_ids: Optional[List[str]] = None
    station_ids: Optional[List[str]] = None
    horizon_hours: int = 24
    model_type: str = "auto"

class ForecastResponse(BaseModel):
    forecast_id: str
    forecast_type: str
    horizon_hours: int
    model_version: str
    accuracy_score: Optional[float]
    forecasts: Dict[str, Any]
    created_at: datetime

class FleetSyncRequest(BaseModel):
    connector_type: str
    config: Dict[str, Any]
    sync_vehicles: bool = True
    sync_routes: bool = True

class FleetSyncResponse(BaseModel):
    sync_id: str
    status: str
    vehicles_synced: int
    routes_synced: int
    errors: List[str]
    created_at: datetime

class OptimizationStatusResponse(BaseModel):
    is_running: bool
    last_run: Optional[datetime]
    next_run: Optional[datetime]
    total_optimizations: int
    success_rate: float
    average_solve_time_ms: float

# --- Optimization Endpoints ---

@router.post("/optimization/run", response_model=OptimizationResponse, status_code=status.HTTP_201_CREATED)
async def run_optimization(
    request: OptimizationRequest,
    background_tasks: BackgroundTasks,
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Run optimization for specified vehicles."""
    try:
        optimization_id = str(uuid.uuid4())
        
        # Get vehicle data
        vehicles = await timescale_client.get_active_vehicles()
        filtered_vehicles = [v for v in vehicles if v["vehicle_id"] in request.vehicle_ids]
        
        if not filtered_vehicles:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No vehicles found")
        
        # Get electricity prices
        prices = await timescale_client.get_latest_prices(
            nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
            start=datetime.now() - timedelta(hours=1)
        )
        
        # Prepare optimization parameters
        if julia_bridge:
            vehicle_data = []
            for vehicle in filtered_vehicles:
                vehicle_info = julia_bridge.prepare_vehicle_data(
                    vehicle_id=vehicle["vehicle_id"],
                    battery_capacity_kwh=vehicle.get("battery_capacity_kwh", 75.0),
                    max_charge_rate_kw=vehicle.get("max_charge_rate_kw", 22.0),
                    max_discharge_rate_kw=vehicle.get("max_discharge_rate_kw", 10.0),
                    initial_soc_kwh=vehicle.get("current_soc_kwh", 30.0),
                    min_soc_kwh=vehicle.get("min_soc_kwh", 15.0),
                    departure_time=None,  # Would get from routes
                    required_soc_kwh=None
                )
                vehicle_data.append(vehicle_info)
            
            # Prepare electricity prices
            electricity_prices = [p.get("lmp_price_mwh", 0.0) / 1000 for p in prices]
            
            params = julia_bridge.prepare_optimization_params(
                horizon_hours=request.horizon_hours,
                timestep_minutes=request.timestep_minutes,
                facility_capacity_kw=request.facility_capacity_kw,
                electricity_prices=electricity_prices,
                cost_weight=request.cost_weight,
                peak_weight=request.peak_weight,
                demand_weight=request.demand_weight
            )
            
            # Run optimization
            result = await julia_bridge.solve_optimization(vehicle_data, params)
            
            if result["status"] == "OPTIMAL":
                # Store optimization result
                await timescale_client.store_optimization_decision({
                    "time": datetime.now(),
                    "optimization_window_start": datetime.now(),
                    "optimization_window_end": datetime.now() + timedelta(hours=request.horizon_hours),
                    "fleet_operator_id": None,
                    "site_id": None,
                    "algorithm_version": "julia-mip-v1",
                    "objective_function": "multi_objective",
                    "objective_value": result.get("objective_value"),
                    "computation_time_ms": result.get("solve_time_ms", 0),
                    "constraints_satisfied": True,
                    "decision_payload": {
                        "optimization_id": optimization_id,
                        "request": request.dict(),
                        "result": result
                    },
                    "sync_status": "completed"
                })
                
                return OptimizationResponse(
                    optimization_id=optimization_id,
                    status=result["status"],
                    objective_value=result.get("objective_value", 0.0),
                    solve_time_ms=result.get("solve_time_ms", 0.0),
                    total_cost=result.get("total_cost", 0.0),
                    peak_demand_kw=result.get("peak_demand_kw", 0.0),
                    charging_schedules=result.get("charging_schedule", []),
                    created_at=datetime.now()
                )
            else:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Optimization failed: {result.get('message', 'Unknown error')}")
        
        else:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Julia bridge not available")
    
    except Exception as e:
        logger.error(f"Error running optimization: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.get("/optimization/status", response_model=OptimizationStatusResponse)
async def get_optimization_status(timescale_client: TimescaleClient = Depends(get_timescale_client)):
    """Get current optimization status."""
    try:
        status_data = await timescale_client.get_optimization_status()
        
        return OptimizationStatusResponse(
            is_running=status_data.get("is_running", False),
            last_run=status_data.get("last_run"),
            next_run=status_data.get("next_run"),
            total_optimizations=status_data.get("total_optimizations", 0),
            success_rate=status_data.get("success_rate", 0.0),
            average_solve_time_ms=status_data.get("average_solve_time_ms", 0.0)
        )
    
    except Exception as e:
        logger.error(f"Error getting optimization status: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# --- Forecasting Endpoints ---

@router.post("/forecasts/price", response_model=ForecastResponse, status_code=status.HTTP_201_CREATED)
async def generate_price_forecasts(
    request: ForecastRequest,
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Generate price forecasts for specified nodes."""
    try:
        forecast_id = str(uuid.uuid4())
        
        if not price_forecaster:
            price_forecaster = PriceForecaster(timescale_client)
        
        node_ids = request.node_ids or ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"]
        
        forecasts = await price_forecaster.batch_forecast(node_ids, request.horizon_hours)
        
        if forecasts:
            # Calculate average accuracy score
            accuracy_scores = [f.accuracy_score for f in forecasts.values() if f.accuracy_score is not None]
            avg_accuracy = sum(accuracy_scores) / len(accuracy_scores) if accuracy_scores else None
            
            # Get model version from first forecast
            model_version = list(forecasts.values())[0].model_version if forecasts else "unknown"
            
            return ForecastResponse(
                forecast_id=forecast_id,
                forecast_type="price",
                horizon_hours=request.horizon_hours,
                model_version=model_version,
                accuracy_score=avg_accuracy,
                forecasts={node_id: {
                    "forecast_prices": forecast.forecast_prices,
                    "confidence_intervals": forecast.confidence_intervals,
                    "model_version": forecast.model_version
                } for node_id, forecast in forecasts.items()},
                created_at=datetime.now()
            )
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to generate price forecasts")
    
    except Exception as e:
        logger.error(f"Error generating price forecasts: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.post("/forecasts/demand", response_model=ForecastResponse, status_code=status.HTTP_201_CREATED)
async def generate_demand_forecasts(
    request: ForecastRequest,
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Generate demand forecasts for specified stations."""
    try:
        forecast_id = str(uuid.uuid4())
        
        if not demand_forecaster:
            demand_forecaster = DemandForecaster(timescale_client)
        
        station_ids = request.station_ids or ["default_station"]
        
        forecasts = await demand_forecaster.batch_forecast(station_ids, request.horizon_hours)
        
        if forecasts:
            # Calculate average accuracy score
            accuracy_scores = [f.accuracy_score for f in forecasts.values() if f.accuracy_score is not None]
            avg_accuracy = sum(accuracy_scores) / len(accuracy_scores) if accuracy_scores else None
            
            # Get model version from first forecast
            model_version = list(forecasts.values())[0].model_version if forecasts else "unknown"
            
            return ForecastResponse(
                forecast_id=forecast_id,
                forecast_type="demand",
                horizon_hours=request.horizon_hours,
                model_version=model_version,
                accuracy_score=avg_accuracy,
                forecasts={station_id: {
                    "forecast_demand": forecast.forecast_demand,
                    "confidence_intervals": forecast.confidence_intervals,
                    "model_version": forecast.model_version
                } for station_id, forecast in forecasts.items()},
                created_at=datetime.now()
            )
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to generate demand forecasts")
    
    except Exception as e:
        logger.error(f"Error generating demand forecasts: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# --- Fleet Management Endpoints ---

@router.post("/fleet/sync", response_model=FleetSyncResponse, status_code=status.HTTP_201_CREATED)
async def sync_fleet_data(
    request: FleetSyncRequest,
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Sync fleet data from external system."""
    try:
        sync_id = str(uuid.uuid4())
        
        if not fleet_manager:
            fleet_manager = FleetConnectorManager()
        
        # Create connector
        connector = create_connector(request.connector_type, request.config)
        fleet_manager.register_connector("temp", connector)
        
        vehicles_synced = 0
        routes_synced = 0
        errors = []
        
        try:
            async with connector:
                # Sync vehicles
                if request.sync_vehicles:
                    vehicles = await connector.get_vehicles()
                    for vehicle in vehicles:
                        vehicle_data = {
                            "vehicle_id": vehicle.vehicle_id,
                            "station_id": "default_station",
                            "battery_capacity_kwh": vehicle.battery_capacity_kwh,
                            "max_charge_rate_kw": vehicle.max_charge_rate_kw,
                            "max_discharge_rate_kw": vehicle.max_discharge_rate_kw,
                            "current_soc_kwh": vehicle.current_soc_kwh,
                            "vehicle_type": "passenger",
                            "make": vehicle.make,
                            "model": vehicle.model,
                            "year": vehicle.year,
                            "is_active": vehicle.status == "active",
                            "created_at": datetime.now(),
                            "updated_at": datetime.now()
                        }
                        await timescale_client.store_vehicle_fleet(vehicle_data)
                        vehicles_synced += 1
                
                # Sync routes
                if request.sync_routes:
                    routes = await connector.get_routes()
                    for route in routes:
                        route_data = {
                            "route_id": route.route_id,
                            "vehicle_id": route.vehicle_id,
                            "station_id": "default_station",
                            "departure_time": route.departure_time,
                            "arrival_time": route.arrival_time,
                            "destination": route.destination,
                            "route_distance_km": route.route_distance_km,
                            "required_soc_percent": route.required_soc_percent,
                            "route_status": route.route_status,
                            "route_priority": route.priority,
                            "created_at": datetime.now(),
                            "updated_at": datetime.now()
                        }
                        await timescale_client.store_vehicle_route(route_data)
                        routes_synced += 1
        
        except Exception as e:
            errors.append(f"Sync error: {str(e)}")
        
        return FleetSyncResponse(
            sync_id=sync_id,
            status="completed" if not errors else "partial",
            vehicles_synced=vehicles_synced,
            routes_synced=routes_synced,
            errors=errors,
            created_at=datetime.now()
        )
    
    except Exception as e:
        logger.error(f"Error syncing fleet data: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# --- System Status Endpoints ---

@router.get("/system/status")
async def get_system_status():
    """Get overall system status."""
    try:
        status = {
            "timestamp": datetime.now(),
            "components": {
                "julia_bridge": julia_bridge is not None,
                "price_forecaster": price_forecaster is not None,
                "demand_forecaster": demand_forecaster is not None,
                "fleet_manager": fleet_manager is not None,
                "optimization_engine": optimization_engine is not None
            },
            "version": "1.0.0",
            "status": "operational"
        }
        
        return status
    
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.post("/system/initialize")
async def initialize_system(
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Initialize system components."""
    try:
        global price_forecaster, demand_forecaster, fleet_manager, julia_bridge
        
        # Initialize components
        price_forecaster = PriceForecaster(timescale_client)
        demand_forecaster = DemandForecaster(timescale_client)
        fleet_manager = FleetConnectorManager()
        julia_bridge = JuliaBridge()
        
        # Test Julia bridge
        julia_available = await julia_bridge.test_solver()
        
        return {
            "status": "initialized",
            "components": {
                "price_forecaster": True,
                "demand_forecaster": True,
                "fleet_manager": True,
                "julia_bridge": julia_available
            },
            "timestamp": datetime.now()
        }
    
    except Exception as e:
        logger.error(f"Error initializing system: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# --- Analytics Endpoints ---

@router.get("/analytics/optimization-history")
async def get_optimization_history(
    days: int = 7,
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Get optimization history for analytics."""
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        # This would query optimization decisions from TimescaleDB
        # For now, return mock data
        history = {
            "period": {"start": start_time, "end": end_time},
            "total_optimizations": 42,
            "success_rate": 0.95,
            "average_solve_time_ms": 1250.0,
            "total_cost_savings": 1250.50,
            "peak_demand_reduction": 0.15,
            "daily_stats": [
                {
                    "date": (end_time - timedelta(days=i)).date(),
                    "optimizations": 6,
                    "avg_solve_time_ms": 1200.0,
                    "cost_savings": 45.50,
                    "peak_reduction": 0.12
                }
                for i in range(days)
            ]
        }
        
        return history
    
    except Exception as e:
        logger.error(f"Error getting optimization history: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@router.get("/analytics/forecast-accuracy")
async def get_forecast_accuracy(
    days: int = 7,
    timescale_client: TimescaleClient = Depends(get_timescale_client)
):
    """Get forecast accuracy metrics."""
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        # This would query forecast accuracy from TimescaleDB
        # For now, return mock data
        accuracy = {
            "period": {"start": start_time, "end": end_time},
            "price_forecasts": {
                "total_forecasts": 168,  # 7 days * 24 hours
                "average_accuracy": 0.87,
                "model_breakdown": {
                    "prophet": {"count": 120, "accuracy": 0.89},
                    "arima": {"count": 35, "accuracy": 0.82},
                    "fallback": {"count": 13, "accuracy": 0.75}
                }
            },
            "demand_forecasts": {
                "total_forecasts": 168,
                "average_accuracy": 0.91,
                "model_breakdown": {
                    "lstm": {"count": 100, "accuracy": 0.93},
                    "xgboost": {"count": 50, "accuracy": 0.88},
                    "fallback": {"count": 18, "accuracy": 0.85}
                }
            }
        }
        
        return accuracy
    
    except Exception as e:
        logger.error(f"Error getting forecast accuracy: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
