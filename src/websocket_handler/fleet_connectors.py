"""Fleet management system connectors for route data integration."""

import asyncio
import aiohttp
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
import logging

from .monitoring import get_logger


@dataclass
class VehicleData:
    """Standardized vehicle data structure."""
    vehicle_id: str
    make: str
    model: str
    year: int
    battery_capacity_kwh: float
    max_charge_rate_kw: float
    max_discharge_rate_kw: float
    current_soc_kwh: float
    location: Optional[Tuple[float, float]] = None  # (lat, lon)
    status: str = "active"


@dataclass
class RouteData:
    """Standardized route data structure."""
    route_id: str
    vehicle_id: str
    departure_time: datetime
    arrival_time: Optional[datetime]
    destination: str
    route_distance_km: float
    required_soc_percent: float
    route_status: str = "scheduled"
    priority: int = 1


class BaseFleetConnector(ABC):
    """Abstract base class for fleet management system connectors."""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize connector with configuration."""
        self.config = config
        self.logger = get_logger(__name__)
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
    
    @abstractmethod
    async def authenticate(self) -> bool:
        """Authenticate with the fleet management system."""
        pass
    
    @abstractmethod
    async def get_vehicles(self) -> List[VehicleData]:
        """Get all vehicles from the fleet management system."""
        pass
    
    @abstractmethod
    async def get_routes(self, vehicle_id: Optional[str] = None) -> List[RouteData]:
        """Get routes from the fleet management system."""
        pass
    
    @abstractmethod
    async def update_route(self, route_data: RouteData) -> bool:
        """Update a route in the fleet management system."""
        pass
    
    @abstractmethod
    async def cancel_route(self, route_id: str) -> bool:
        """Cancel a route in the fleet management system."""
        pass


class GeotabConnector(BaseFleetConnector):
    """Geotab fleet management system connector."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = config.get("base_url", "https://my.geotab.com/apiv1")
        self.username = config.get("username")
        self.password = config.get("password")
        self.database = config.get("database")
        self.session_token: Optional[str] = None
    
    async def authenticate(self) -> bool:
        """Authenticate with Geotab using credentials."""
        try:
            auth_data = {
                "userName": self.username,
                "password": self.password,
                "database": self.database
            }
            
            async with self.session.post(f"{self.base_url}/Authenticate", json=auth_data) as response:
                if response.status == 200:
                    result = await response.json()
                    self.session_token = result.get("credentials", {}).get("sessionId")
                    self.logger.info("Geotab authentication successful")
                    return True
                else:
                    self.logger.error(f"Geotab authentication failed: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Geotab authentication error: {e}")
            return False
    
    async def get_vehicles(self) -> List[VehicleData]:
        """Get all vehicles from Geotab."""
        try:
            if not self.session_token:
                await self.authenticate()
            
            params = {
                "credentials": {"sessionId": self.session_token},
                "typeName": "Device"
            }
            
            async with self.session.post(f"{self.base_url}/Get", json=params) as response:
                if response.status == 200:
                    result = await response.json()
                    devices = result.get("result", [])
                    
                    vehicles = []
                    for device in devices:
                        vehicle = VehicleData(
                            vehicle_id=device.get("id"),
                            make=device.get("vehicleIdentificationNumber", "").split()[0] if device.get("vehicleIdentificationNumber") else "Unknown",
                            model=device.get("name", "Unknown"),
                            year=device.get("year", 2020),
                            battery_capacity_kwh=device.get("batteryCapacity", 75.0),
                            max_charge_rate_kw=device.get("maxChargeRate", 22.0),
                            max_discharge_rate_kw=device.get("maxDischargeRate", 10.0),
                            current_soc_kwh=device.get("currentSOC", 50.0),
                            status="active" if device.get("isActive") else "inactive"
                        )
                        vehicles.append(vehicle)
                    
                    self.logger.info(f"Retrieved {len(vehicles)} vehicles from Geotab")
                    return vehicles
                else:
                    self.logger.error(f"Failed to get vehicles from Geotab: {response.status}")
                    return []
                    
        except Exception as e:
            self.logger.error(f"Error getting vehicles from Geotab: {e}")
            return []
    
    async def get_routes(self, vehicle_id: Optional[str] = None) -> List[RouteData]:
        """Get routes from Geotab."""
        try:
            if not self.session_token:
                await self.authenticate()
            
            params = {
                "credentials": {"sessionId": self.session_token},
                "typeName": "Trip"
            }
            
            if vehicle_id:
                params["search"] = {"device": {"id": vehicle_id}}
            
            async with self.session.post(f"{self.base_url}/Get", json=params) as response:
                if response.status == 200:
                    result = await response.json()
                    trips = result.get("result", [])
                    
                    routes = []
                    for trip in trips:
                        route = RouteData(
                            route_id=trip.get("id"),
                            vehicle_id=trip.get("device", {}).get("id"),
                            departure_time=datetime.fromisoformat(trip.get("start", "").replace("Z", "+00:00")),
                            arrival_time=datetime.fromisoformat(trip.get("stop", "").replace("Z", "+00:00")) if trip.get("stop") else None,
                            destination=trip.get("destination", "Unknown"),
                            route_distance_km=trip.get("distance", 0.0) / 1000,  # Convert meters to km
                            required_soc_percent=80.0,  # Default value
                            route_status="completed" if trip.get("stop") else "in_progress"
                        )
                        routes.append(route)
                    
                    self.logger.info(f"Retrieved {len(routes)} routes from Geotab")
                    return routes
                else:
                    self.logger.error(f"Failed to get routes from Geotab: {response.status}")
                    return []
                    
        except Exception as e:
            self.logger.error(f"Error getting routes from Geotab: {e}")
            return []
    
    async def update_route(self, route_data: RouteData) -> bool:
        """Update a route in Geotab."""
        try:
            if not self.session_token:
                await self.authenticate()
            
            # Geotab doesn't directly support route updates, so we'll log this
            self.logger.info(f"Route update requested for {route_data.route_id} - Geotab doesn't support direct route updates")
            return True
            
        except Exception as e:
            self.logger.error(f"Error updating route in Geotab: {e}")
            return False
    
    async def cancel_route(self, route_id: str) -> bool:
        """Cancel a route in Geotab."""
        try:
            if not self.session_token:
                await self.authenticate()
            
            # Geotab doesn't directly support route cancellation, so we'll log this
            self.logger.info(f"Route cancellation requested for {route_id} - Geotab doesn't support direct route cancellation")
            return True
            
        except Exception as e:
            self.logger.error(f"Error cancelling route in Geotab: {e}")
            return False


class SamsaraConnector(BaseFleetConnector):
    """Samsara fleet management system connector."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = config.get("base_url", "https://api.samsara.com/fleet")
        self.api_token = config.get("api_token")
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
    
    async def authenticate(self) -> bool:
        """Authenticate with Samsara using API token."""
        try:
            async with self.session.get(f"{self.base_url}/vehicles", headers=self.headers) as response:
                if response.status == 200:
                    self.logger.info("Samsara authentication successful")
                    return True
                else:
                    self.logger.error(f"Samsara authentication failed: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Samsara authentication error: {e}")
            return False
    
    async def get_vehicles(self) -> List[VehicleData]:
        """Get all vehicles from Samsara."""
        try:
            async with self.session.get(f"{self.base_url}/vehicles", headers=self.headers) as response:
                if response.status == 200:
                    result = await response.json()
                    vehicles_data = result.get("data", [])
                    
                    vehicles = []
                    for vehicle_data in vehicles_data:
                        vehicle = VehicleData(
                            vehicle_id=vehicle_data.get("id"),
                            make=vehicle_data.get("make", "Unknown"),
                            model=vehicle_data.get("model", "Unknown"),
                            year=vehicle_data.get("year", 2020),
                            battery_capacity_kwh=vehicle_data.get("batteryCapacity", 75.0),
                            max_charge_rate_kw=vehicle_data.get("maxChargeRate", 22.0),
                            max_discharge_rate_kw=vehicle_data.get("maxDischargeRate", 10.0),
                            current_soc_kwh=vehicle_data.get("currentSOC", 50.0),
                            status="active" if vehicle_data.get("isActive") else "inactive"
                        )
                        vehicles.append(vehicle)
                    
                    self.logger.info(f"Retrieved {len(vehicles)} vehicles from Samsara")
                    return vehicles
                else:
                    self.logger.error(f"Failed to get vehicles from Samsara: {response.status}")
                    return []
                    
        except Exception as e:
            self.logger.error(f"Error getting vehicles from Samsara: {e}")
            return []
    
    async def get_routes(self, vehicle_id: Optional[str] = None) -> List[RouteData]:
        """Get routes from Samsara."""
        try:
            url = f"{self.base_url}/routes"
            if vehicle_id:
                url += f"?vehicleId={vehicle_id}"
            
            async with self.session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    result = await response.json()
                    routes_data = result.get("data", [])
                    
                    routes = []
                    for route_data in routes_data:
                        route = RouteData(
                            route_id=route_data.get("id"),
                            vehicle_id=route_data.get("vehicleId"),
                            departure_time=datetime.fromisoformat(route_data.get("startTime", "").replace("Z", "+00:00")),
                            arrival_time=datetime.fromisoformat(route_data.get("endTime", "").replace("Z", "+00:00")) if route_data.get("endTime") else None,
                            destination=route_data.get("destination", "Unknown"),
                            route_distance_km=route_data.get("distance", 0.0),
                            required_soc_percent=route_data.get("requiredSOC", 80.0),
                            route_status=route_data.get("status", "scheduled")
                        )
                        routes.append(route)
                    
                    self.logger.info(f"Retrieved {len(routes)} routes from Samsara")
                    return routes
                else:
                    self.logger.error(f"Failed to get routes from Samsara: {response.status}")
                    return []
                    
        except Exception as e:
            self.logger.error(f"Error getting routes from Samsara: {e}")
            return []
    
    async def update_route(self, route_data: RouteData) -> bool:
        """Update a route in Samsara."""
        try:
            update_data = {
                "vehicleId": route_data.vehicle_id,
                "startTime": route_data.departure_time.isoformat(),
                "destination": route_data.destination,
                "distance": route_data.route_distance_km,
                "requiredSOC": route_data.required_soc_percent
            }
            
            async with self.session.put(f"{self.base_url}/routes/{route_data.route_id}", 
                                      json=update_data, headers=self.headers) as response:
                if response.status == 200:
                    self.logger.info(f"Successfully updated route {route_data.route_id} in Samsara")
                    return True
                else:
                    self.logger.error(f"Failed to update route in Samsara: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Error updating route in Samsara: {e}")
            return False
    
    async def cancel_route(self, route_id: str) -> bool:
        """Cancel a route in Samsara."""
        try:
            async with self.session.delete(f"{self.base_url}/routes/{route_id}", headers=self.headers) as response:
                if response.status == 200:
                    self.logger.info(f"Successfully cancelled route {route_id} in Samsara")
                    return True
                else:
                    self.logger.error(f"Failed to cancel route in Samsara: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Error cancelling route in Samsara: {e}")
            return False


class VerizonConnectConnector(BaseFleetConnector):
    """Verizon Connect fleet management system connector."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = config.get("base_url", "https://api.verizonconnect.com/v4")
        self.api_key = config.get("api_key")
        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }
    
    async def authenticate(self) -> bool:
        """Authenticate with Verizon Connect using API key."""
        try:
            async with self.session.get(f"{self.base_url}/vehicles", headers=self.headers) as response:
                if response.status == 200:
                    self.logger.info("Verizon Connect authentication successful")
                    return True
                else:
                    self.logger.error(f"Verizon Connect authentication failed: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Verizon Connect authentication error: {e}")
            return False
    
    async def get_vehicles(self) -> List[VehicleData]:
        """Get all vehicles from Verizon Connect."""
        try:
            async with self.session.get(f"{self.base_url}/vehicles", headers=self.headers) as response:
                if response.status == 200:
                    result = await response.json()
                    vehicles_data = result.get("vehicles", [])
                    
                    vehicles = []
                    for vehicle_data in vehicles_data:
                        vehicle = VehicleData(
                            vehicle_id=vehicle_data.get("id"),
                            make=vehicle_data.get("make", "Unknown"),
                            model=vehicle_data.get("model", "Unknown"),
                            year=vehicle_data.get("year", 2020),
                            battery_capacity_kwh=vehicle_data.get("batteryCapacity", 75.0),
                            max_charge_rate_kw=vehicle_data.get("maxChargeRate", 22.0),
                            max_discharge_rate_kw=vehicle_data.get("maxDischargeRate", 10.0),
                            current_soc_kwh=vehicle_data.get("currentSOC", 50.0),
                            status="active" if vehicle_data.get("isActive") else "inactive"
                        )
                        vehicles.append(vehicle)
                    
                    self.logger.info(f"Retrieved {len(vehicles)} vehicles from Verizon Connect")
                    return vehicles
                else:
                    self.logger.error(f"Failed to get vehicles from Verizon Connect: {response.status}")
                    return []
                    
        except Exception as e:
            self.logger.error(f"Error getting vehicles from Verizon Connect: {e}")
            return []
    
    async def get_routes(self, vehicle_id: Optional[str] = None) -> List[RouteData]:
        """Get routes from Verizon Connect."""
        try:
            url = f"{self.base_url}/scheduled-routes"
            if vehicle_id:
                url += f"?vehicleId={vehicle_id}"
            
            async with self.session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    result = await response.json()
                    routes_data = result.get("routes", [])
                    
                    routes = []
                    for route_data in routes_data:
                        route = RouteData(
                            route_id=route_data.get("id"),
                            vehicle_id=route_data.get("vehicleId"),
                            departure_time=datetime.fromisoformat(route_data.get("startTime", "").replace("Z", "+00:00")),
                            arrival_time=datetime.fromisoformat(route_data.get("endTime", "").replace("Z", "+00:00")) if route_data.get("endTime") else None,
                            destination=route_data.get("destination", "Unknown"),
                            route_distance_km=route_data.get("distance", 0.0),
                            required_soc_percent=route_data.get("requiredSOC", 80.0),
                            route_status=route_data.get("status", "scheduled")
                        )
                        routes.append(route)
                    
                    self.logger.info(f"Retrieved {len(routes)} routes from Verizon Connect")
                    return routes
                else:
                    self.logger.error(f"Failed to get routes from Verizon Connect: {response.status}")
                    return []
                    
        except Exception as e:
            self.logger.error(f"Error getting routes from Verizon Connect: {e}")
            return []
    
    async def update_route(self, route_data: RouteData) -> bool:
        """Update a route in Verizon Connect."""
        try:
            update_data = {
                "vehicleId": route_data.vehicle_id,
                "startTime": route_data.departure_time.isoformat(),
                "destination": route_data.destination,
                "distance": route_data.route_distance_km,
                "requiredSOC": route_data.required_soc_percent
            }
            
            async with self.session.put(f"{self.base_url}/scheduled-routes/{route_data.route_id}", 
                                      json=update_data, headers=self.headers) as response:
                if response.status == 200:
                    self.logger.info(f"Successfully updated route {route_data.route_id} in Verizon Connect")
                    return True
                else:
                    self.logger.error(f"Failed to update route in Verizon Connect: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Error updating route in Verizon Connect: {e}")
            return False
    
    async def cancel_route(self, route_id: str) -> bool:
        """Cancel a route in Verizon Connect."""
        try:
            async with self.session.delete(f"{self.base_url}/scheduled-routes/{route_id}", headers=self.headers) as response:
                if response.status == 200:
                    self.logger.info(f"Successfully cancelled route {route_id} in Verizon Connect")
                    return True
                else:
                    self.logger.error(f"Failed to cancel route in Verizon Connect: {response.status}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Error cancelling route in Verizon Connect: {e}")
            return False


class MockConnector(BaseFleetConnector):
    """Mock connector for testing with synthetic data."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.vehicles: List[VehicleData] = []
        self.routes: List[RouteData] = []
        self._initialize_mock_data()
    
    def _initialize_mock_data(self):
        """Initialize mock data for testing."""
        # Create mock vehicles
        for i in range(10):
            vehicle = VehicleData(
                vehicle_id=f"mock_vehicle_{i+1}",
                make=["Tesla", "Nissan", "BMW", "Ford"][i % 4],
                model=f"Model {i+1}",
                year=2020 + (i % 4),
                battery_capacity_kwh=50.0 + (i * 10),
                max_charge_rate_kw=11.0 + (i * 2),
                max_discharge_rate_kw=5.0 + (i * 1),
                current_soc_kwh=30.0 + (i * 5),
                status="active"
            )
            self.vehicles.append(vehicle)
        
        # Create mock routes
        for i in range(15):
            route = RouteData(
                route_id=f"mock_route_{i+1}",
                vehicle_id=f"mock_vehicle_{(i % 10) + 1}",
                departure_time=datetime.now(timezone.utc) + timedelta(hours=i),
                destination=f"Destination {i+1}",
                route_distance_km=10.0 + (i * 5),
                required_soc_percent=80.0,
                route_status="scheduled"
            )
            self.routes.append(route)
    
    async def authenticate(self) -> bool:
        """Mock authentication always succeeds."""
        self.logger.info("Mock connector authentication successful")
        return True
    
    async def get_vehicles(self) -> List[VehicleData]:
        """Get mock vehicles."""
        self.logger.info(f"Retrieved {len(self.vehicles)} mock vehicles")
        return self.vehicles.copy()
    
    async def get_routes(self, vehicle_id: Optional[str] = None) -> List[RouteData]:
        """Get mock routes."""
        if vehicle_id:
            filtered_routes = [r for r in self.routes if r.vehicle_id == vehicle_id]
            self.logger.info(f"Retrieved {len(filtered_routes)} mock routes for vehicle {vehicle_id}")
            return filtered_routes
        else:
            self.logger.info(f"Retrieved {len(self.routes)} mock routes")
            return self.routes.copy()
    
    async def update_route(self, route_data: RouteData) -> bool:
        """Update mock route."""
        for i, route in enumerate(self.routes):
            if route.route_id == route_data.route_id:
                self.routes[i] = route_data
                self.logger.info(f"Updated mock route {route_data.route_id}")
                return True
        return False
    
    async def cancel_route(self, route_id: str) -> bool:
        """Cancel mock route."""
        for i, route in enumerate(self.routes):
            if route.route_id == route_id:
                self.routes[i].route_status = "cancelled"
                self.logger.info(f"Cancelled mock route {route_id}")
                return True
        return False


class FleetConnectorManager:
    """Manager for multiple fleet connectors."""
    
    def __init__(self):
        self.connectors: Dict[str, BaseFleetConnector] = {}
        self.logger = get_logger(__name__)
    
    def register_connector(self, name: str, connector: BaseFleetConnector):
        """Register a fleet connector."""
        self.connectors[name] = connector
        self.logger.info(f"Registered fleet connector: {name}")
    
    async def get_all_vehicles(self) -> List[VehicleData]:
        """Get vehicles from all registered connectors."""
        all_vehicles = []
        
        for name, connector in self.connectors.items():
            try:
                async with connector:
                    vehicles = await connector.get_vehicles()
                    all_vehicles.extend(vehicles)
            except Exception as e:
                self.logger.error(f"Error getting vehicles from {name}: {e}")
        
        return all_vehicles
    
    async def get_all_routes(self) -> List[RouteData]:
        """Get routes from all registered connectors."""
        all_routes = []
        
        for name, connector in self.connectors.items():
            try:
                async with connector:
                    routes = await connector.get_routes()
                    all_routes.extend(routes)
            except Exception as e:
                self.logger.error(f"Error getting routes from {name}: {e}")
        
        return all_routes
    
    async def sync_with_timescale(self, timescale_client):
        """Sync fleet data with TimescaleDB."""
        try:
            # Get all vehicles and routes
            vehicles = await self.get_all_vehicles()
            routes = await self.get_all_routes()
            
            # Store vehicles
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
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc)
                }
                await timescale_client.store_vehicle_fleet(vehicle_data)
            
            # Store routes
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
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc)
                }
                await timescale_client.store_vehicle_route(route_data)
            
            self.logger.info(f"Synced {len(vehicles)} vehicles and {len(routes)} routes with TimescaleDB")
            
        except Exception as e:
            self.logger.error(f"Error syncing fleet data: {e}")


# Factory function for creating connectors
def create_connector(connector_type: str, config: Dict[str, Any]) -> BaseFleetConnector:
    """Create a fleet connector based on type."""
    if connector_type == "geotab":
        return GeotabConnector(config)
    elif connector_type == "samsara":
        return SamsaraConnector(config)
    elif connector_type == "verizon_connect":
        return VerizonConnectConnector(config)
    elif connector_type == "mock":
        return MockConnector(config)
    else:
        raise ValueError(f"Unknown connector type: {connector_type}")
