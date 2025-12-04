"""Fleet management API endpoints for route updates and vehicle scheduling."""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from uuid import uuid4
import json

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field, validator

from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .julia_bridge import JuliaBridge


logger = get_logger(__name__)

# Pydantic models for API
class RouteUpdate(BaseModel):
    """Route update request model."""
    vehicle_id: str = Field(..., description="Unique vehicle identifier")
    departure_time: datetime = Field(..., description="Scheduled departure time")
    destination: Optional[str] = Field(None, description="Destination address or location")
    route_distance_km: Optional[float] = Field(None, description="Total route distance in kilometers")
    required_soc_percent: float = Field(80.0, ge=0, le=100, description="Required SOC percentage at departure")
    route_priority: int = Field(1, ge=1, le=10, description="Route priority (1=low, 10=high)")
    estimated_duration_hours: Optional[float] = Field(None, description="Estimated trip duration")
    
    @validator('departure_time')
    def validate_departure_time(cls, v):
        if v <= datetime.now(timezone.utc):
            raise ValueError('Departure time must be in the future')
        return v

class BulkRouteUpdate(BaseModel):
    """Bulk route update request model."""
    routes: List[RouteUpdate] = Field(..., description="List of route updates")
    update_mode: str = Field("replace", description="Update mode: replace, append, or merge")

class RouteOverride(BaseModel):
    """Route override request model."""
    override_type: str = Field(..., description="Type of override: priority, emergency, or manual")
    new_departure_time: Optional[datetime] = Field(None, description="New departure time")
    new_required_soc_percent: Optional[float] = Field(None, ge=0, le=100, description="New required SOC")
    reason: str = Field(..., description="Reason for override")
    operator_id: str = Field(..., description="Operator performing override")

class RouteResponse(BaseModel):
    """Route response model."""
    route_id: str
    vehicle_id: str
    station_id: str
    departure_time: datetime
    arrival_time: Optional[datetime]
    destination: Optional[str]
    route_distance_km: Optional[float]
    required_soc_percent: float
    actual_soc_percent: Optional[float]
    route_status: str
    created_at: datetime
    updated_at: datetime

class OptimizationStatus(BaseModel):
    """Optimization status response model."""
    is_running: bool
    last_run_time: Optional[datetime]
    next_run_time: Optional[datetime]
    active_vehicles: int
    pending_routes: int
    solver_status: str
    last_objective_value: Optional[float]
    last_solve_time_ms: Optional[float]


class FleetAPI:
    """Fleet management API handler."""
    
    def __init__(self, timescale_client: TimescaleClient, julia_bridge: JuliaBridge):
        self.timescale_client = timescale_client
        self.julia_bridge = julia_bridge
        self.logger = get_logger(__name__)
        self.router = APIRouter(prefix="/api/v1", tags=["fleet"])
        self._setup_routes()
    
    def _setup_routes(self):
        """Setup API routes."""
        
        @self.router.post("/routes/update", response_model=Dict[str, Any])
        async def update_route(route: RouteUpdate, background_tasks: BackgroundTasks):
            """Update a single route."""
            try:
                route_id = str(uuid4())
                
                # Store route in database
                await self.timescale_client.store_vehicle_route({
                    "route_id": route_id,
                    "vehicle_id": route.vehicle_id,
                    "station_id": "default_station",  # TODO: Get from vehicle mapping
                    "departure_time": route.departure_time,
                    "destination": route.destination,
                    "route_distance_km": route.route_distance_km,
                    "required_soc_percent": route.required_soc_percent,
                    "route_status": "scheduled",
                    "created_at": datetime.now(timezone.utc)
                })
                
                # Trigger optimization in background
                background_tasks.add_task(self._trigger_optimization, "route_update")
                
                self.logger.info(f"Route updated for vehicle {route.vehicle_id}")
                
                return {
                    "status": "success",
                    "route_id": route_id,
                    "message": "Route updated successfully"
                }
                
            except Exception as e:
                self.logger.error(f"Error updating route: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.post("/routes/bulk", response_model=Dict[str, Any])
        async def bulk_update_routes(update: BulkRouteUpdate, background_tasks: BackgroundTasks):
            """Update multiple routes in bulk."""
            try:
                route_ids = []
                
                for route in update.routes:
                    route_id = str(uuid4())
                    
                    await self.timescale_client.store_vehicle_route({
                        "route_id": route_id,
                        "vehicle_id": route.vehicle_id,
                        "station_id": "default_station",
                        "departure_time": route.departure_time,
                        "destination": route.destination,
                        "route_distance_km": route.route_distance_km,
                        "required_soc_percent": route.required_soc_percent,
                        "route_status": "scheduled",
                        "created_at": datetime.now(timezone.utc)
                    })
                    
                    route_ids.append(route_id)
                
                # Trigger optimization in background
                background_tasks.add_task(self._trigger_optimization, "bulk_route_update")
                
                self.logger.info(f"Bulk updated {len(route_ids)} routes")
                
                return {
                    "status": "success",
                    "route_ids": route_ids,
                    "count": len(route_ids),
                    "message": f"Updated {len(route_ids)} routes successfully"
                }
                
            except Exception as e:
                self.logger.error(f"Error in bulk route update: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.get("/routes/{vehicle_id}", response_model=List[RouteResponse])
        async def get_vehicle_routes(vehicle_id: str):
            """Get all routes for a specific vehicle."""
            try:
                routes = await self.timescale_client.get_vehicle_routes(vehicle_id)
                
                return [
                    RouteResponse(
                        route_id=route["route_id"],
                        vehicle_id=route["vehicle_id"],
                        station_id=route["station_id"],
                        departure_time=route["departure_time"],
                        arrival_time=route.get("arrival_time"),
                        destination=route.get("destination"),
                        route_distance_km=route.get("route_distance_km"),
                        required_soc_percent=route["required_soc_percent"],
                        actual_soc_percent=route.get("actual_soc_percent"),
                        route_status=route["route_status"],
                        created_at=route["created_at"],
                        updated_at=route.get("updated_at", route["created_at"])
                    )
                    for route in routes
                ]
                
            except Exception as e:
                self.logger.error(f"Error getting routes for vehicle {vehicle_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.put("/routes/{vehicle_id}/override", response_model=Dict[str, Any])
        async def override_route(vehicle_id: str, override: RouteOverride, background_tasks: BackgroundTasks):
            """Override a route with manual adjustments."""
            try:
                # Get existing route
                routes = await self.timescale_client.get_vehicle_routes(vehicle_id)
                if not routes:
                    raise HTTPException(status_code=404, detail="No routes found for vehicle")
                
                # Get the most recent route
                latest_route = max(routes, key=lambda r: r["created_at"])
                
                # Update route with override
                update_data = {
                    "route_id": latest_route["route_id"],
                    "override_type": override.override_type,
                    "override_reason": override.reason,
                    "operator_id": override.operator_id,
                    "updated_at": datetime.now(timezone.utc)
                }
                
                if override.new_departure_time:
                    update_data["departure_time"] = override.new_departure_time
                
                if override.new_required_soc_percent:
                    update_data["required_soc_percent"] = override.new_required_soc_percent
                
                await self.timescale_client.update_vehicle_route(update_data)
                
                # Trigger optimization in background
                background_tasks.add_task(self._trigger_optimization, "route_override")
                
                self.logger.info(f"Route overridden for vehicle {vehicle_id} by {override.operator_id}")
                
                return {
                    "status": "success",
                    "route_id": latest_route["route_id"],
                    "message": "Route override applied successfully"
                }
                
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error overriding route for vehicle {vehicle_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.delete("/routes/{vehicle_id}", response_model=Dict[str, Any])
        async def cancel_route(vehicle_id: str, background_tasks: BackgroundTasks):
            """Cancel all routes for a vehicle."""
            try:
                # Update route status to cancelled
                await self.timescale_client.cancel_vehicle_routes(vehicle_id)
                
                # Trigger optimization in background
                background_tasks.add_task(self._trigger_optimization, "route_cancellation")
                
                self.logger.info(f"Routes cancelled for vehicle {vehicle_id}")
                
                return {
                    "status": "success",
                    "message": f"All routes cancelled for vehicle {vehicle_id}"
                }
                
            except Exception as e:
                self.logger.error(f"Error cancelling routes for vehicle {vehicle_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.get("/optimization/status", response_model=OptimizationStatus)
        async def get_optimization_status():
            """Get current optimization status."""
            try:
                # Get optimization status from database
                status = await self.timescale_client.get_optimization_status()
                
                return OptimizationStatus(
                    is_running=status.get("is_running", False),
                    last_run_time=status.get("last_run_time"),
                    next_run_time=status.get("next_run_time"),
                    active_vehicles=status.get("active_vehicles", 0),
                    pending_routes=status.get("pending_routes", 0),
                    solver_status=status.get("solver_status", "unknown"),
                    last_objective_value=status.get("last_objective_value"),
                    last_solve_time_ms=status.get("last_solve_time_ms")
                )
                
            except Exception as e:
                self.logger.error(f"Error getting optimization status: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.post("/optimization/trigger", response_model=Dict[str, Any])
        async def trigger_optimization(background_tasks: BackgroundTasks):
            """Manually trigger optimization."""
            try:
                background_tasks.add_task(self._trigger_optimization, "manual_trigger")
                
                return {
                    "status": "success",
                    "message": "Optimization triggered successfully"
                }
                
            except Exception as e:
                self.logger.error(f"Error triggering optimization: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.router.get("/vehicles", response_model=List[Dict[str, Any]])
        async def get_vehicles():
            """Get all vehicles with their current status."""
            try:
                vehicles = await self.timescale_client.get_all_vehicles()
                return vehicles
                
            except Exception as e:
                self.logger.error(f"Error getting vehicles: {e}")
                raise HTTPException(status_code=500, detail=str(e))
    
    async def _trigger_optimization(self, trigger_reason: str):
        """Trigger optimization in background."""
        try:
            self.logger.info(f"Triggering optimization due to: {trigger_reason}")
            
            # Get active vehicles and routes
            vehicles = await self.timescale_client.get_active_vehicles()
            routes = await self.timescale_client.get_active_routes()
            
            if not vehicles:
                self.logger.info("No active vehicles found, skipping optimization")
                return
            
            # Prepare vehicle data for Julia solver
            vehicle_data = []
            for vehicle in vehicles:
                # Find route for this vehicle
                vehicle_route = next((r for r in routes if r["vehicle_id"] == vehicle["vehicle_id"]), None)
                
                vehicle_info = self.julia_bridge.prepare_vehicle_data(
                    vehicle_id=vehicle["vehicle_id"],
                    battery_capacity_kwh=vehicle.get("battery_capacity_kwh", 75.0),
                    max_charge_rate_kw=vehicle.get("max_charge_rate_kw", 22.0),
                    max_discharge_rate_kw=vehicle.get("max_discharge_rate_kw", 10.0),
                    initial_soc_kwh=vehicle.get("current_soc_kwh", 30.0),
                    min_soc_kwh=vehicle.get("min_soc_kwh", 15.0),
                    departure_time=datetime.fromisoformat(vehicle_route["departure_time"]) if vehicle_route else None,
                    required_soc_kwh=vehicle_route["required_soc_percent"] * vehicle.get("battery_capacity_kwh", 75.0) / 100 if vehicle_route else None
                )
                vehicle_data.append(vehicle_info)
            
            # Get electricity prices
            prices = await self.timescale_client.get_latest_prices(
                nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
                start=datetime.now(timezone.utc)
            )
            
            # Prepare optimization parameters
            electricity_prices = [p.get("lmp_price_mwh", 0.12) / 1000 for p in prices]  # Convert to $/kWh
            if not electricity_prices:
                electricity_prices = [0.12] * 96  # Default price for 24h with 15-min intervals
            
            params = self.julia_bridge.prepare_optimization_params(
                horizon_hours=24,
                timestep_minutes=15,
                facility_capacity_kw=1000.0,
                electricity_prices=electricity_prices
            )
            
            # Solve optimization
            result = await self.julia_bridge.solve_optimization(vehicle_data, params)
            
            # Store optimization result
            await self.timescale_client.store_optimization_decision({
                "time": datetime.now(timezone.utc),
                "optimization_window_start": datetime.now(timezone.utc),
                "optimization_window_end": datetime.now(timezone.utc).replace(hour=23, minute=59),
                "fleet_operator_id": None,
                "site_id": None,
                "algorithm_version": "julia-mip-v1",
                "objective_function": "cost_minimization_with_peak_shaving",
                "objective_value": result.get("objective_value"),
                "computation_time_ms": result.get("solve_time_ms"),
                "constraints_satisfied": result.get("status") == "OPTIMAL",
                "decision_payload": result,
                "sync_status": "completed",
                "trigger_reason": trigger_reason
            })
            
            self.logger.info(f"Optimization completed: {result.get('status')}")
            
        except Exception as e:
            self.logger.error(f"Error in background optimization: {e}")


# Dependency injection
def get_fleet_api(timescale_client: TimescaleClient = Depends(), julia_bridge: JuliaBridge = Depends()) -> FleetAPI:
    """Get FleetAPI instance."""
    return FleetAPI(timescale_client, julia_bridge)
