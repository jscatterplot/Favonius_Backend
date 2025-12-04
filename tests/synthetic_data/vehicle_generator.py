"""Synthetic data generators for testing EV fleet optimization."""

import random
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
import uuid


@dataclass
class VehicleProfile:
    """Vehicle profile for synthetic data generation."""
    vehicle_id: str
    battery_capacity_kwh: float
    max_charge_rate_kw: float
    max_discharge_rate_kw: float
    vehicle_type: str
    make: str
    model: str
    year: int
    charge_efficiency: float
    discharge_efficiency: float


@dataclass
class RouteProfile:
    """Route profile for synthetic data generation."""
    vehicle_id: str
    departure_time: datetime
    destination: str
    route_distance_km: float
    required_soc_percent: float
    estimated_duration_hours: float
    route_priority: int


class VehicleGenerator:
    """Generate synthetic vehicle fleet data."""
    
    def __init__(self, seed: int = 42):
        """Initialize vehicle generator with random seed."""
        random.seed(seed)
        np.random.seed(seed)
        
        # Vehicle type distributions
        self.vehicle_types = {
            "sedan": {"weight": 0.4, "battery_range": (40, 80), "charge_range": (7, 22)},
            "suv": {"weight": 0.3, "battery_range": (60, 100), "charge_range": (11, 22)},
            "delivery_van": {"weight": 0.2, "battery_range": (50, 90), "charge_range": (22, 50)},
            "bus": {"weight": 0.1, "battery_range": (200, 400), "charge_range": (50, 150)}
        }
        
        # Make and model mappings
        self.makes_models = {
            "Tesla": ["Model 3", "Model Y", "Model S", "Model X"],
            "Nissan": ["Leaf", "Ariya"],
            "BMW": ["i3", "iX", "i4"],
            "Ford": ["Mustang Mach-E", "F-150 Lightning"],
            "Volkswagen": ["ID.4", "ID.Buzz"],
            "Mercedes": ["EQS", "EQC"],
            "Audi": ["e-tron", "Q4 e-tron"],
            "Hyundai": ["Ioniq 5", "Kona Electric"],
            "Kia": ["EV6", "Niro EV"],
            "Rivian": ["R1T", "R1S"]
        }
    
    def generate_vehicle_fleet(self, fleet_size: int = 50) -> List[VehicleProfile]:
        """Generate a fleet of synthetic vehicles.
        
        Args:
            fleet_size: Number of vehicles to generate
            
        Returns:
            List of VehicleProfile objects
        """
        vehicles = []
        
        for i in range(fleet_size):
            # Select vehicle type based on weights
            vehicle_type = self._select_vehicle_type()
            
            # Generate vehicle specifications
            battery_capacity = self._generate_battery_capacity(vehicle_type)
            charge_rate = self._generate_charge_rate(vehicle_type)
            discharge_rate = min(charge_rate * 0.5, 20.0)  # V2G typically 50% of charge rate
            
            # Select make and model
            make, model = self._select_make_model()
            year = random.randint(2019, 2024)
            
            # Generate efficiencies
            charge_efficiency = random.uniform(0.92, 0.98)
            discharge_efficiency = random.uniform(0.88, 0.95)
            
            vehicle = VehicleProfile(
                vehicle_id=f"vehicle_{i+1:03d}",
                battery_capacity_kwh=battery_capacity,
                max_charge_rate_kw=charge_rate,
                max_discharge_rate_kw=discharge_rate,
                vehicle_type=vehicle_type,
                make=make,
                model=model,
                year=year,
                charge_efficiency=charge_efficiency,
                discharge_efficiency=discharge_efficiency
            )
            
            vehicles.append(vehicle)
        
        return vehicles
    
    def _select_vehicle_type(self) -> str:
        """Select vehicle type based on weights."""
        types = list(self.vehicle_types.keys())
        weights = [self.vehicle_types[t]["weight"] for t in types]
        return random.choices(types, weights=weights)[0]
    
    def _generate_battery_capacity(self, vehicle_type: str) -> float:
        """Generate battery capacity based on vehicle type."""
        battery_range = self.vehicle_types[vehicle_type]["battery_range"]
        return round(random.uniform(battery_range[0], battery_range[1]), 1)
    
    def _generate_charge_rate(self, vehicle_type: str) -> float:
        """Generate charge rate based on vehicle type."""
        charge_range = self.vehicle_types[vehicle_type]["charge_range"]
        return round(random.uniform(charge_range[0], charge_range[1]), 1)
    
    def _select_make_model(self) -> Tuple[str, str]:
        """Select make and model randomly."""
        make = random.choice(list(self.makes_models.keys()))
        model = random.choice(self.makes_models[make])
        return make, model


class RouteGenerator:
    """Generate synthetic route schedules."""
    
    def __init__(self, seed: int = 42):
        """Initialize route generator with random seed."""
        random.seed(seed)
        np.random.seed(seed)
        
        # Route patterns
        self.departure_patterns = {
            "morning_rush": {"start_hour": 7, "end_hour": 9, "weight": 0.4},
            "midday": {"start_hour": 10, "end_hour": 14, "weight": 0.2},
            "evening_rush": {"start_hour": 16, "end_hour": 18, "weight": 0.3},
            "night": {"start_hour": 20, "end_hour": 22, "weight": 0.1}
        }
        
        # Destination types
        self.destinations = {
            "office": {"weight": 0.3, "distance_range": (5, 50), "duration_range": (0.5, 2.0)},
            "warehouse": {"weight": 0.2, "distance_range": (10, 100), "duration_range": (1.0, 4.0)},
            "retail": {"weight": 0.2, "distance_range": (3, 30), "duration_range": (0.5, 1.5)},
            "residential": {"weight": 0.2, "distance_range": (2, 25), "duration_range": (0.3, 1.0)},
            "service": {"weight": 0.1, "distance_range": (5, 80), "duration_range": (1.0, 3.0)}
        }
        
        # SOC requirements based on trip type
        self.soc_requirements = {
            "office": (0.7, 0.9),
            "warehouse": (0.8, 1.0),
            "retail": (0.6, 0.8),
            "residential": (0.5, 0.7),
            "service": (0.8, 1.0)
        }
    
    def generate_daily_routes(
        self, 
        vehicles: List[VehicleProfile], 
        date: datetime,
        routes_per_vehicle: int = 2
    ) -> List[RouteProfile]:
        """Generate daily routes for a fleet of vehicles.
        
        Args:
            vehicles: List of vehicles to generate routes for
            date: Date to generate routes for
            routes_per_vehicle: Average number of routes per vehicle
            
        Returns:
            List of RouteProfile objects
        """
        routes = []
        
        for vehicle in vehicles:
            # Determine number of routes for this vehicle (1-3)
            num_routes = random.choices([1, 2, 3], weights=[0.2, 0.6, 0.2])[0]
            
            # Generate routes for this vehicle
            vehicle_routes = self._generate_vehicle_routes(vehicle, date, num_routes)
            routes.extend(vehicle_routes)
        
        # Sort routes by departure time
        routes.sort(key=lambda r: r.departure_time)
        
        return routes
    
    def _generate_vehicle_routes(
        self, 
        vehicle: VehicleProfile, 
        date: datetime, 
        num_routes: int
    ) -> List[RouteProfile]:
        """Generate routes for a single vehicle."""
        routes = []
        current_time = date.replace(hour=6, minute=0, second=0, microsecond=0)
        
        for i in range(num_routes):
            # Select departure pattern
            pattern = self._select_departure_pattern()
            
            # Generate departure time within pattern
            departure_hour = random.randint(pattern["start_hour"], pattern["end_hour"])
            departure_minute = random.randint(0, 59)
            departure_time = current_time.replace(hour=departure_hour, minute=departure_minute)
            
            # Ensure departure time is in the future
            if departure_time <= datetime.now(timezone.utc):
                departure_time += timedelta(days=1)
            
            # Select destination type
            dest_type = self._select_destination_type()
            
            # Generate route parameters
            distance = self._generate_distance(dest_type)
            duration = self._generate_duration(dest_type)
            required_soc = self._generate_required_soc(dest_type)
            priority = self._generate_priority()
            
            # Generate destination name
            destination = self._generate_destination_name(dest_type, distance)
            
            route = RouteProfile(
                vehicle_id=vehicle.vehicle_id,
                departure_time=departure_time,
                destination=destination,
                route_distance_km=distance,
                required_soc_percent=required_soc,
                estimated_duration_hours=duration,
                route_priority=priority
            )
            
            routes.append(route)
            
            # Update current time for next route (add dwell time)
            dwell_hours = random.uniform(2, 8)
            current_time = departure_time + timedelta(hours=duration + dwell_hours)
        
        return routes
    
    def _select_departure_pattern(self) -> Dict[str, Any]:
        """Select departure pattern based on weights."""
        patterns = list(self.departure_patterns.values())
        weights = [p["weight"] for p in patterns]
        return random.choices(patterns, weights=weights)[0]
    
    def _select_destination_type(self) -> str:
        """Select destination type based on weights."""
        dest_types = list(self.destinations.keys())
        weights = [self.destinations[t]["weight"] for t in dest_types]
        return random.choices(dest_types, weights=weights)[0]
    
    def _generate_distance(self, dest_type: str) -> float:
        """Generate route distance based on destination type."""
        distance_range = self.destinations[dest_type]["distance_range"]
        return round(random.uniform(distance_range[0], distance_range[1]), 1)
    
    def _generate_duration(self, dest_type: str) -> float:
        """Generate trip duration based on destination type."""
        duration_range = self.destinations[dest_type]["duration_range"]
        return round(random.uniform(duration_range[0], duration_range[1]), 1)
    
    def _generate_required_soc(self, dest_type: str) -> float:
        """Generate required SOC based on destination type."""
        soc_range = self.soc_requirements[dest_type]
        return round(random.uniform(soc_range[0], soc_range[1]) * 100, 1)
    
    def _generate_priority(self) -> int:
        """Generate route priority."""
        return random.choices([1, 2, 3, 4, 5], weights=[0.1, 0.2, 0.4, 0.2, 0.1])[0]
    
    def _generate_destination_name(self, dest_type: str, distance: float) -> str:
        """Generate destination name based on type and distance."""
        if dest_type == "office":
            return f"Office Building {random.randint(1, 50)}"
        elif dest_type == "warehouse":
            return f"Distribution Center {random.randint(1, 20)}"
        elif dest_type == "retail":
            return f"Shopping Center {random.randint(1, 30)}"
        elif dest_type == "residential":
            return f"Residential Area {random.randint(1, 100)}"
        elif dest_type == "service":
            return f"Service Center {random.randint(1, 15)}"
        else:
            return f"Destination {random.randint(1, 100)}"


class SyntheticDataManager:
    """Manager for synthetic data generation and storage."""
    
    def __init__(self, timescale_client, seed: int = 42):
        """Initialize synthetic data manager."""
        self.timescale_client = timescale_client
        self.vehicle_generator = VehicleGenerator(seed)
        self.route_generator = RouteGenerator(seed)
        self.logger = get_logger(__name__)
    
    async def generate_and_store_fleet(
        self, 
        fleet_size: int = 50,
        station_id: str = "default_station"
    ) -> List[VehicleProfile]:
        """Generate and store a synthetic vehicle fleet."""
        vehicles = self.vehicle_generator.generate_vehicle_fleet(fleet_size)
        
        for vehicle in vehicles:
            vehicle_data = {
                "vehicle_id": vehicle.vehicle_id,
                "station_id": station_id,
                "battery_capacity_kwh": vehicle.battery_capacity_kwh,
                "max_charge_rate_kw": vehicle.max_charge_rate_kw,
                "max_discharge_rate_kw": vehicle.max_discharge_rate_kw,
                "current_soc_kwh": random.uniform(20, 80),  # Random initial SOC
                "min_soc_kwh": 15.0,
                "charge_efficiency": vehicle.charge_efficiency,
                "discharge_efficiency": vehicle.discharge_efficiency,
                "vehicle_type": vehicle.vehicle_type,
                "make": vehicle.make,
                "model": vehicle.model,
                "year": vehicle.year,
                "is_active": True,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_vehicle_fleet(vehicle_data)
        
        self.logger.info(f"Generated and stored {len(vehicles)} vehicles")
        return vehicles
    
    async def generate_and_store_routes(
        self, 
        vehicles: List[VehicleProfile],
        date: datetime,
        routes_per_vehicle: int = 2
    ) -> List[RouteProfile]:
        """Generate and store synthetic routes."""
        routes = self.route_generator.generate_daily_routes(vehicles, date, routes_per_vehicle)
        
        for route in routes:
            route_data = {
                "route_id": str(uuid.uuid4()),
                "vehicle_id": route.vehicle_id,
                "station_id": "default_station",
                "departure_time": route.departure_time,
                "destination": route.destination,
                "route_distance_km": route.route_distance_km,
                "required_soc_percent": route.required_soc_percent,
                "route_status": "scheduled",
                "route_priority": route.route_priority,
                "estimated_duration_hours": route.estimated_duration_hours,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_vehicle_route(route_data)
        
        self.logger.info(f"Generated and stored {len(routes)} routes")
        return routes
    
    async def generate_test_scenario(
        self,
        fleet_size: int = 20,
        station_id: str = "test_station",
        date: datetime = None
    ) -> Dict[str, Any]:
        """Generate a complete test scenario with vehicles and routes."""
        if date is None:
            date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Generate vehicles
        vehicles = await self.generate_and_store_fleet(fleet_size, station_id)
        
        # Generate routes
        routes = await self.generate_and_store_routes(vehicles, date)
        
        return {
            "vehicles": vehicles,
            "routes": routes,
            "station_id": station_id,
            "date": date,
            "fleet_size": fleet_size
        }
    
    def generate_price_scenario(self, base_price: float = 0.12) -> List[float]:
        """Generate synthetic electricity price scenario."""
        # Generate 24-hour price profile with realistic patterns
        prices = []
        
        for hour in range(24):
            # Base price with time-of-day variation
            if 6 <= hour <= 8:  # Morning peak
                price_multiplier = 1.5
            elif 17 <= hour <= 19:  # Evening peak
                price_multiplier = 1.8
            elif 22 <= hour or hour <= 5:  # Night valley
                price_multiplier = 0.6
            else:  # Off-peak
                price_multiplier = 1.0
            
            # Add some randomness
            price_multiplier *= random.uniform(0.9, 1.1)
            
            price = base_price * price_multiplier
            prices.append(round(price, 4))
        
        return prices


# Convenience functions for testing
async def create_test_fleet(timescale_client, fleet_size: int = 20) -> Dict[str, Any]:
    """Create a test fleet with vehicles and routes."""
    manager = SyntheticDataManager(timescale_client)
    return await manager.generate_test_scenario(fleet_size)


def generate_test_prices(base_price: float = 0.12) -> List[float]:
    """Generate test electricity prices."""
    manager = SyntheticDataManager(42)
    return manager.generate_price_scenario(base_price)
