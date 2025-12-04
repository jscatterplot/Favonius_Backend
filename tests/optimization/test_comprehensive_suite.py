"""Comprehensive test suite for the EV fleet optimization and forecasting system."""

import asyncio
import pytest
import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

from src.websocket_handler.julia_bridge import JuliaBridge
from src.websocket_handler.price_forecaster import PriceForecaster, PriceForecastScheduler
from src.websocket_handler.demand_forecaster import DemandForecaster, DemandForecastScheduler
from src.websocket_handler.fleet_connectors import FleetConnectorManager, MockConnector
from tests.synthetic_data.vehicle_generator import SyntheticDataManager, create_test_fleet, generate_test_prices


class TestJuliaMIPSolver:
    """Test suite for Julia MIP solver."""
    
    @pytest.fixture
    async def julia_bridge(self):
        """Create Julia bridge instance."""
        bridge = JuliaBridge()
        # Test Julia installation
        if not await bridge.test_solver():
            pytest.skip("Julia solver not available")
        return bridge
    
    @pytest.fixture
    def simple_vehicle_data(self):
        """Create simple vehicle data for testing."""
        return [
            {
                "vehicle_id": "test_vehicle_1",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 60.0
            }
        ]
    
    @pytest.fixture
    def simple_optimization_params(self):
        """Create simple optimization parameters."""
        return {
            "horizon_hours": 4,
            "timestep_minutes": 15,
            "facility_capacity_kw": 50.0,
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": [0.10, 0.12, 0.15, 0.12, 0.10, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.18, 0.15, 0.12, 0.10, 0.08],
            "forecast_demand": [100.0] * 16,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
    
    @pytest.mark.asyncio
    async def test_solver_basic_functionality(self, julia_bridge, simple_vehicle_data, simple_optimization_params):
        """Test basic solver functionality."""
        result = await julia_bridge.solve_optimization(simple_vehicle_data, simple_optimization_params)
        
        assert result["status"] == "OPTIMAL"
        assert "objective_value" in result
        assert "solve_time_ms" in result
        assert "charging_schedule" in result
        assert "soc_trajectory" in result
        assert result["solve_time_ms"] > 0
    
    @pytest.mark.asyncio
    async def test_solver_constraint_satisfaction(self, julia_bridge, simple_vehicle_data, simple_optimization_params):
        """Test that solver satisfies constraints."""
        result = await julia_bridge.solve_optimization(simple_vehicle_data, simple_optimization_params)
        
        if result["status"] == "OPTIMAL":
            charging_schedule = result["charging_schedule"]
            soc_trajectory = result["soc_trajectory"]
            
            # Check SOC bounds
            for timestep in soc_trajectory[0]:  # First vehicle
                assert 15.0 <= timestep <= 75.0  # min_soc <= soc <= battery_capacity
            
            # Check charging power bounds
            for timestep in charging_schedule[0]:  # First vehicle
                assert 0 <= timestep <= 22.0  # 0 <= power <= max_charge_rate
            
            # Check departure readiness
            departure_timestep = 32  # 8 hours * 4 timesteps per hour
            if departure_timestep < len(soc_trajectory[0]):
                assert soc_trajectory[0][departure_timestep] >= 60.0  # required_soc
    
    @pytest.mark.asyncio
    async def test_solver_multiple_vehicles(self, julia_bridge):
        """Test solver with multiple vehicles."""
        vehicles = [
            {
                "vehicle_id": f"vehicle_{i}",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 60.0
            }
            for i in range(5)
        ]
        
        params = {
            "horizon_hours": 4,
            "timestep_minutes": 15,
            "facility_capacity_kw": 100.0,  # Increased capacity for 5 vehicles
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": [0.10] * 16,
            "forecast_demand": [100.0] * 16,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        result = await julia_bridge.solve_optimization(vehicles, params)
        
        assert result["status"] == "OPTIMAL"
        assert len(result["charging_schedule"]) == 5
        assert len(result["soc_trajectory"]) == 5
    
    @pytest.mark.asyncio
    async def test_solver_peak_demand_constraint(self, julia_bridge):
        """Test that solver respects facility capacity constraints."""
        vehicles = [
            {
                "vehicle_id": f"vehicle_{i}",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 60.0
            }
            for i in range(10)
        ]
        
        params = {
            "horizon_hours": 4,
            "timestep_minutes": 15,
            "facility_capacity_kw": 50.0,  # Low capacity - should constrain charging
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": [0.10] * 16,
            "forecast_demand": [100.0] * 16,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        result = await julia_bridge.solve_optimization(vehicles, params)
        
        if result["status"] == "OPTIMAL":
            charging_schedule = result["charging_schedule"]
            
            # Check that total charging power doesn't exceed facility capacity
            for timestep in range(len(charging_schedule[0])):
                total_power = sum(charging_schedule[v][timestep] for v in range(len(vehicles)))
                assert total_power <= 50.0  # facility_capacity_kw
    
    @pytest.mark.asyncio
    async def test_solver_price_optimization(self, julia_bridge):
        """Test that solver optimizes for electricity prices."""
        vehicles = [
            {
                "vehicle_id": "vehicle_1",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 60.0
            }
        ]
        
        # Create price profile with clear low-price periods
        prices = [0.20] * 4 + [0.05] * 4 + [0.20] * 4 + [0.05] * 4  # Low prices in middle periods
        
        params = {
            "horizon_hours": 4,
            "timestep_minutes": 15,
            "facility_capacity_kw": 50.0,
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": prices,
            "forecast_demand": [100.0] * 16,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        result = await julia_bridge.solve_optimization(vehicles, params)
        
        if result["status"] == "OPTIMAL":
            charging_schedule = result["charging_schedule"][0]
            
            # Check that more charging happens during low-price periods
            high_price_charging = sum(charging_schedule[i] for i in range(4)) + sum(charging_schedule[i] for i in range(8, 12))
            low_price_charging = sum(charging_schedule[i] for i in range(4, 8)) + sum(charging_schedule[i] for i in range(12, 16))
            
            # Low price periods should have more charging
            assert low_price_charging >= high_price_charging * 0.8  # Allow some tolerance
    
    @pytest.mark.asyncio
    async def test_solver_v2g_functionality(self, julia_bridge):
        """Test V2G (discharging) functionality."""
        vehicles = [
            {
                "vehicle_id": "vehicle_1",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 60.0,  # Higher initial SOC
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 50.0  # Lower required SOC - allows discharging
            }
        ]
        
        # High prices to encourage V2G
        prices = [0.30] * 16
        
        params = {
            "horizon_hours": 4,
            "timestep_minutes": 15,
            "facility_capacity_kw": 50.0,
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": prices,
            "forecast_demand": [100.0] * 16,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        result = await julia_bridge.solve_optimization(vehicles, params)
        
        if result["status"] == "OPTIMAL":
            discharging_schedule = result["discharging_schedule"][0]
            
            # Should have some discharging during high-price periods
            total_discharging = sum(discharging_schedule)
            assert total_discharging > 0  # Some V2G should occur
    
    @pytest.mark.asyncio
    async def test_solver_performance(self, julia_bridge):
        """Test solver performance with larger fleet."""
        # Create larger fleet
        vehicles = []
        for i in range(20):
            vehicles.append({
                "vehicle_id": f"vehicle_{i}",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 60.0
            })
        
        params = {
            "horizon_hours": 24,
            "timestep_minutes": 15,
            "facility_capacity_kw": 500.0,
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": [0.12] * 96,  # 24 hours * 4 timesteps per hour
            "forecast_demand": [100.0] * 96,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        result = await julia_bridge.solve_optimization(vehicles, params, timeout_seconds=60)
        
        # Should complete within reasonable time
        assert result["solve_time_ms"] < 30000  # Less than 30 seconds
        assert result["status"] in ["OPTIMAL", "FEASIBLE"]
    
    @pytest.mark.asyncio
    async def test_solver_error_handling(self, julia_bridge):
        """Test solver error handling."""
        # Test with invalid data
        invalid_vehicles = [
            {
                "vehicle_id": "invalid_vehicle",
                "battery_capacity_kwh": -10.0,  # Invalid negative capacity
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": None,
                "required_soc_kwh": 60.0
            }
        ]
        
        params = {
            "horizon_hours": 4,
            "timestep_minutes": 15,
            "facility_capacity_kw": 50.0,
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": [0.12] * 16,
            "forecast_demand": [100.0] * 16,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        result = await julia_bridge.solve_optimization(invalid_vehicles, params)
        
        # Should handle errors gracefully
        assert result["status"] in ["ERROR", "INFEASIBLE", "UNBOUNDED"]
    
    @pytest.mark.asyncio
    async def test_solver_timeout(self, julia_bridge):
        """Test solver timeout handling."""
        # Create a very large problem
        vehicles = []
        for i in range(100):  # Large fleet
            vehicles.append({
                "vehicle_id": f"vehicle_{i}",
                "battery_capacity_kwh": 75.0,
                "max_charge_rate_kw": 22.0,
                "max_discharge_rate_kw": 10.0,
                "initial_soc_kwh": 30.0,
                "min_soc_kwh": 15.0,
                "charge_efficiency": 0.95,
                "discharge_efficiency": 0.90,
                "departure_time": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
                "required_soc_kwh": 60.0
            })
        
        params = {
            "horizon_hours": 24,
            "timestep_minutes": 15,
            "facility_capacity_kw": 2000.0,
            "demand_charge_rate_per_kw": 15.0,
            "electricity_prices": [0.12] * 96,
            "forecast_demand": [100.0] * 96,
            "cost_weight": 1.0,
            "peak_weight": 0.1,
            "demand_weight": 0.05
        }
        
        # Set very short timeout
        result = await julia_bridge.solve_optimization(vehicles, params, timeout_seconds=1)
        
        # Should timeout gracefully
        assert result["status"] == "TIMEOUT"
    
    def test_vehicle_data_preparation(self, julia_bridge):
        """Test vehicle data preparation."""
        vehicle_data = julia_bridge.prepare_vehicle_data(
            vehicle_id="test_vehicle",
            battery_capacity_kwh=75.0,
            max_charge_rate_kw=22.0,
            max_discharge_rate_kw=10.0,
            initial_soc_kwh=30.0,
            min_soc_kwh=15.0,
            departure_time=datetime.now(timezone.utc) + timedelta(hours=8),
            required_soc_kwh=60.0
        )
        
        assert vehicle_data["vehicle_id"] == "test_vehicle"
        assert vehicle_data["battery_capacity_kwh"] == 75.0
        assert vehicle_data["max_charge_rate_kw"] == 22.0
        assert vehicle_data["initial_soc_kwh"] == 30.0
        assert vehicle_data["required_soc_kwh"] == 60.0
        assert vehicle_data["departure_time"] is not None
    
    def test_optimization_params_preparation(self, julia_bridge):
        """Test optimization parameters preparation."""
        params = julia_bridge.prepare_optimization_params(
            horizon_hours=24,
            timestep_minutes=15,
            facility_capacity_kw=1000.0,
            electricity_prices=[0.12] * 96
        )
        
        assert params["horizon_hours"] == 24
        assert params["timestep_minutes"] == 15
        assert params["facility_capacity_kw"] == 1000.0
        assert len(params["electricity_prices"]) == 96
        assert params["cost_weight"] == 1.0
        assert params["peak_weight"] == 0.1
        assert params["demand_weight"] == 0.05


class TestPriceForecasting:
    """Test suite for price forecasting."""
    
    @pytest.fixture
    async def mock_timescale_client(self):
        """Create mock TimescaleDB client."""
        class MockTimescaleClient:
            async def get_latest_prices(self, nodes, start, end=None):
                # Return mock price data
                prices = []
                current_time = start
                while current_time < (end or start + timedelta(days=1)):
                    prices.append({
                        "time": current_time,
                        "lmp_price_mwh": 120.0 + 20.0 * (current_time.hour % 12) / 12  # Simulate price fluctuations
                    })
                    current_time += timedelta(hours=1)
                return prices
            
            async def store_price_forecast(self, forecast_data):
                pass
        
        return MockTimescaleClient()
    
    @pytest.mark.asyncio
    async def test_price_forecaster_initialization(self, mock_timescale_client):
        """Test price forecaster initialization."""
        forecaster = PriceForecaster(mock_timescale_client)
        assert forecaster is not None
        assert forecaster.model_configs is not None
    
    @pytest.mark.asyncio
    async def test_price_forecast_generation(self, mock_timescale_client):
        """Test price forecast generation."""
        forecaster = PriceForecaster(mock_timescale_client)
        
        forecast = await forecaster.generate_forecast("TH_SP15_GEN-APND", horizon_hours=24)
        
        if forecast:
            assert forecast.node_id == "TH_SP15_GEN-APND"
            assert len(forecast.forecast_prices) == 24
            assert forecast.horizon_hours == 24
            assert forecast.model_version in ["prophet", "arima", "fallback"]
    
    @pytest.mark.asyncio
    async def test_price_forecast_scheduler(self, mock_timescale_client):
        """Test price forecast scheduler."""
        forecaster = PriceForecaster(mock_timescale_client)
        scheduler = PriceForecastScheduler(forecaster)
        
        # Start scheduler
        await scheduler.start(interval_hours=1)
        
        # Let it run for a short time
        await asyncio.sleep(2)
        
        # Stop scheduler
        await scheduler.stop()
        
        assert not scheduler.running


class TestDemandForecasting:
    """Test suite for demand forecasting."""
    
    @pytest.fixture
    async def mock_timescale_client(self):
        """Create mock TimescaleDB client."""
        class MockTimescaleClient:
            async def get_telemetry_data(self, station_id, start_time, end_time):
                # Return mock telemetry data
                data = []
                current_time = start_time
                while current_time < end_time:
                    data.append({
                        "time": current_time,
                        "power_kw": 50.0 + 20.0 * (current_time.hour % 12) / 12,  # Simulate demand patterns
                        "energy_kwh": 1.0,
                        "voltage_v": 240.0,
                        "current_a": 20.0,
                        "temperature_c": 25.0
                    })
                    current_time += timedelta(minutes=15)
                return data
            
            async def store_demand_forecast(self, forecast_data):
                pass
        
        return MockTimescaleClient()
    
    @pytest.mark.asyncio
    async def test_demand_forecaster_initialization(self, mock_timescale_client):
        """Test demand forecaster initialization."""
        forecaster = DemandForecaster(mock_timescale_client)
        assert forecaster is not None
        assert forecaster.model_configs is not None
    
    @pytest.mark.asyncio
    async def test_demand_forecast_generation(self, mock_timescale_client):
        """Test demand forecast generation."""
        forecaster = DemandForecaster(mock_timescale_client)
        
        forecast = await forecaster.generate_forecast("test_station", horizon_hours=24)
        
        if forecast:
            assert forecast.station_id == "test_station"
            assert len(forecast.forecast_demand) == 24
            assert forecast.horizon_hours == 24
            assert forecast.model_version in ["lstm", "xgboost", "fallback"]
    
    @pytest.mark.asyncio
    async def test_demand_forecast_scheduler(self, mock_timescale_client):
        """Test demand forecast scheduler."""
        forecaster = DemandForecaster(mock_timescale_client)
        scheduler = DemandForecastScheduler(forecaster)
        
        # Start scheduler
        await scheduler.start(interval_hours=1)
        
        # Let it run for a short time
        await asyncio.sleep(2)
        
        # Stop scheduler
        await scheduler.stop()
        
        assert not scheduler.running


class TestFleetConnectors:
    """Test suite for fleet connectors."""
    
    @pytest.mark.asyncio
    async def test_mock_connector(self):
        """Test mock connector functionality."""
        connector = MockConnector({})
        
        async with connector:
            # Test authentication
            auth_result = await connector.authenticate()
            assert auth_result == True
            
            # Test getting vehicles
            vehicles = await connector.get_vehicles()
            assert len(vehicles) == 10
            assert all(v.vehicle_id.startswith("mock_vehicle_") for v in vehicles)
            
            # Test getting routes
            routes = await connector.get_routes()
            assert len(routes) == 15
            assert all(r.route_id.startswith("mock_route_") for r in routes)
            
            # Test route filtering
            vehicle_routes = await connector.get_routes("mock_vehicle_1")
            assert len(vehicle_routes) > 0
    
    @pytest.mark.asyncio
    async def test_fleet_connector_manager(self):
        """Test fleet connector manager."""
        manager = FleetConnectorManager()
        
        # Register mock connector
        mock_connector = MockConnector({})
        manager.register_connector("mock", mock_connector)
        
        # Test getting all vehicles
        vehicles = await manager.get_all_vehicles()
        assert len(vehicles) == 10
        
        # Test getting all routes
        routes = await manager.get_all_routes()
        assert len(routes) == 15


class TestSyntheticDataIntegration:
    """Test integration with synthetic data generators."""
    
    @pytest.mark.asyncio
    async def test_synthetic_fleet_generation(self):
        """Test synthetic fleet generation."""
        from tests.synthetic_data.vehicle_generator import VehicleGenerator
        
        generator = VehicleGenerator(seed=42)
        vehicles = generator.generate_vehicle_fleet(10)
        
        assert len(vehicles) == 10
        assert all(v.battery_capacity_kwh > 0 for v in vehicles)
        assert all(v.max_charge_rate_kw > 0 for v in vehicles)
        assert all(v.vehicle_type in ["sedan", "suv", "delivery_van", "bus"] for v in vehicles)
    
    @pytest.mark.asyncio
    async def test_synthetic_routes_generation(self):
        """Test synthetic routes generation."""
        from tests.synthetic_data.vehicle_generator import VehicleGenerator, RouteGenerator
        
        vehicle_generator = VehicleGenerator(seed=42)
        vehicles = vehicle_generator.generate_vehicle_fleet(5)
        
        route_generator = RouteGenerator(seed=42)
        date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        routes = route_generator.generate_daily_routes(vehicles, date)
        
        assert len(routes) > 0
        assert all(r.route_distance_km > 0 for r in routes)
        assert all(r.required_soc_percent > 0 for r in routes)
        assert all(r.departure_time > datetime.now(timezone.utc) for r in routes)
    
    def test_price_scenario_generation(self):
        """Test price scenario generation."""
        prices = generate_test_prices(base_price=0.12)
        
        assert len(prices) == 24
        assert all(p > 0 for p in prices)
        # Check for realistic price patterns
        assert min(prices) < 0.12  # Should have low prices
        assert max(prices) > 0.12  # Should have high prices


class TestEndToEndIntegration:
    """End-to-end integration tests."""
    
    @pytest.mark.asyncio
    async def test_full_optimization_workflow(self):
        """Test complete optimization workflow."""
        # This would test the full workflow from data generation to optimization
        # For now, we'll create a simplified version
        
        # Generate synthetic data
        from tests.synthetic_data.vehicle_generator import VehicleGenerator, RouteGenerator
        
        vehicle_generator = VehicleGenerator(seed=42)
        vehicles = vehicle_generator.generate_vehicle_fleet(5)
        
        route_generator = RouteGenerator(seed=42)
        date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        routes = route_generator.generate_daily_routes(vehicles, date)
        
        # Generate price data
        prices = generate_test_prices(base_price=0.12)
        
        # Test Julia bridge
        bridge = JuliaBridge()
        if await bridge.test_solver():
            # Prepare vehicle data
            vehicle_data = []
            for vehicle in vehicles:
                vehicle_route = next((r for r in routes if r.vehicle_id == vehicle.vehicle_id), None)
                vehicle_info = bridge.prepare_vehicle_data(
                    vehicle_id=vehicle.vehicle_id,
                    battery_capacity_kwh=vehicle.battery_capacity_kwh,
                    max_charge_rate_kw=vehicle.max_charge_rate_kw,
                    max_discharge_rate_kw=vehicle.max_discharge_rate_kw,
                    initial_soc_kwh=vehicle.current_soc_kwh,
                    min_soc_kwh=vehicle.min_soc_kwh,
                    departure_time=vehicle_route.departure_time if vehicle_route else None,
                    required_soc_kwh=vehicle_route.required_soc_percent * vehicle.battery_capacity_kwh / 100 if vehicle_route else None
                )
                vehicle_data.append(vehicle_info)
            
            # Prepare optimization parameters
            params = bridge.prepare_optimization_params(
                horizon_hours=24,
                timestep_minutes=15,
                facility_capacity_kw=200.0,
                electricity_prices=prices
            )
            
            # Solve optimization
            result = await bridge.solve_optimization(vehicle_data, params)
            
            # Verify results
            assert result["status"] == "OPTIMAL"
            assert len(result["charging_schedule"]) == len(vehicles)
            assert len(result["soc_trajectory"]) == len(vehicles)


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
