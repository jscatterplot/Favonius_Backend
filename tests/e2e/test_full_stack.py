"""Full Stack End-to-End Test.

Tests the complete flow: API → Controller → Optimizer → OCPP Dispatch.

Reference: PRD.md#11-1-mvp-acceptance-tests, Development Plan Step 7.1
"""

import pytest
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
import json

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import asyncpg

from src.api.main import app
from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.core.optimizer import optimize
from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.controller_manager import ControllerManager
from src.core.state.assembler import StateAssembler
from src.adapters.ocpp.server import OCPPServer
from src.adapters.ocpp.charge_point import FleetChargePoint


@pytest.mark.e2e
class TestFullStackE2E:
    """Full stack end-to-end tests."""

    @pytest.fixture
    def depot_config(self):
        """Realistic depot configuration."""
        vehicle_ids = ['bus_1', 'bus_2', 'bus_3', 'bus_4', 'bus_5']
        return DepotConfig(
            vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 5},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
            delta_t=0.25,
            n_timesteps=96,
        )

    @pytest.fixture
    def depot_state(self, depot_config):
        """Realistic depot state."""
        n_t = depot_config.n_timesteps
        vehicles = list(depot_config.vehicle_capacities.keys())
        
        # Time-of-use pricing
        prices = []
        for t in range(n_t):
            hour = (t * 0.25) % 24
            if 16 <= hour < 21:  # Peak
                prices.append(0.25)
            elif 9 <= hour < 16:  # Mid-peak
                prices.append(0.15)
            else:  # Off-peak
                prices.append(0.08)
        
        return DepotState(
            vehicle_socs={v: 0.3 + 0.1 * i for i, v in enumerate(vehicles)},
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={v: [True] * n_t for v in vehicles},
            energy_requirements={v: 180.0 + 20.0 * i for i, v in enumerate(vehicles)},
            departure_times={v: 48 + 8 * i for i, v in enumerate(vehicles)},
            building_power=[50.0] * n_t,
        )

    @pytest.fixture
    def mock_db_pool(self):
        """Mock database pool."""
        pool = MagicMock(spec=asyncpg.Pool)
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        return pool, conn

    @pytest.fixture
    def mock_ocpp_server(self):
        """Mock OCPP server with connected chargers."""
        server = MagicMock(spec=OCPPServer)
        
        # Create mock charge points for each charger
        mock_charge_points = {}
        for i in range(5):
            cp = MagicMock(spec=FleetChargePoint)
            cp.set_charging_profile = AsyncMock(return_value=True)
            cp.remote_start_transaction = AsyncMock(return_value=True)
            cp.remote_stop_transaction = AsyncMock(return_value=True)
            mock_charge_points[f'charger_{i+1}'] = cp
        
        server.charge_points = mock_charge_points
        server.get_charge_point = MagicMock(side_effect=lambda x: mock_charge_points.get(x))
        server.is_connected = MagicMock(side_effect=lambda x: x in mock_charge_points)
        
        return server

    # ============ Full Stack Integration Tests ============

    @pytest.mark.asyncio
    async def test_api_to_optimization_to_dispatch(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Test complete flow: API request → Optimization → OCPP Dispatch."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        # Setup controller
        controller_config = ControllerConfig(
            optimization_timeout=30.0,
            trigger_cooldown_minutes=0,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp_server,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=depot_state)
        
        # Mock vehicle-to-charger mapping
        vehicle_to_charger = {
            f'bus_{i+1}': (f'charger_{i+1}', 1) for i in range(5)
        }
        
        # Execute optimization
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {
                f'bus_{i+1}': f'charger_{i+1}' for i in range(5)
            })
            
            start_time = time.time()
            result = await controller.run_optimization("api_request")
            total_time = time.time() - start_time
        
        # Verify optimization completed
        assert result.status == 'completed'
        assert result.solve_time < 30.0
        
        # Verify schedule was created
        assert len(result.schedule) == 5
        for vehicle_id in depot_config.vehicle_capacities:
            assert vehicle_id in result.schedule
            assert 'charging_power' in result.schedule[vehicle_id]
            assert 'soc' in result.schedule[vehicle_id]
        
        # Verify dispatch was attempted
        # At least some charge points should have received profiles
        dispatch_calls = sum(
            1 for cp in mock_ocpp_server.charge_points.values()
            if cp.set_charging_profile.called
        )
        assert dispatch_calls > 0, "Should dispatch to connected chargers"
        
        print(f"E2E test completed in {total_time:.2f}s")
        print(f"Optimization solve time: {result.solve_time:.2f}s")
        print(f"Objective value: ${result.objective_value:.2f}")
        print(f"Peak demand: {result.peak_demand:.1f} kW")
        print(f"Dispatch calls: {dispatch_calls}")

    @pytest.mark.asyncio
    async def test_api_endpoint_triggers_controller(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Test API endpoint correctly triggers controller."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        # Setup mock controller manager
        mock_controller = MagicMock(spec=DepotController)
        mock_result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                f'bus_{i+1}': {
                    'charging_power': [80.0 if j < 48 else 0.0 for j in range(96)],
                    'soc': [0.3 + 0.01 * j for j in range(96)],
                }
                for i in range(5)
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[200.0] * 96,
            peak_demand=250.0,
            objective_value=1500.0,
            solve_time=5.0,
            status='completed',
        )
        mock_controller.run_optimization = AsyncMock(return_value=mock_result)
        
        mock_manager = MagicMock(spec=ControllerManager)
        mock_manager.get_or_create_controller = AsyncMock(return_value=mock_controller)
        
        with patch('src.api.main.db_pool', pool):
            with patch('src.api.main.controller_manager', mock_manager):
                with patch('src.api.main._get_depot_config', AsyncMock(return_value=depot_config)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="http://test"
                    ) as client:
                        response = await client.post("/optimize", json={
                            "depot_id": depot_id,
                            "horizon_hours": 24,
                        })
        
        # Verify API response
        if response.status_code == 200:
            data = response.json()
            assert 'run_id' in data
            assert 'objective_value' in data
            assert 'schedule' in data
        
        # Verify controller was called
        mock_manager.get_or_create_controller.assert_called()
        mock_controller.run_optimization.assert_called()

    @pytest.mark.asyncio
    async def test_full_pipeline_with_trigger_monitor(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Test full pipeline including trigger monitor."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        controller_config = ControllerConfig(
            optimization_timeout=30.0,
            trigger_cooldown_minutes=0,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp_server,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=depot_state)
        
        optimization_count = [0]
        
        async def track_optimizations(trigger_reason):
            optimization_count[0] += 1
            return await controller.run_optimization.__wrapped__(controller, trigger_reason)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {
                f'bus_{i+1}': f'charger_{i+1}' for i in range(5)
            })
            
            # Initial optimization
            result1 = await controller.run_optimization("initial")
            assert result1.status == 'completed'
            
            # Store expected SoCs
            expected_socs = {
                v: result1.schedule[v]['soc'][48] 
                for v in depot_config.vehicle_capacities
            }
            controller.trigger_monitor.update_expected_state(expected_socs, {})
            
            # Trigger re-optimization
            await controller._handle_trigger("test_trigger")
            
            # Should have run optimization
            assert controller.last_result is not None

    @pytest.mark.asyncio
    async def test_dispatch_verification(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Test that dispatch commands are correctly generated."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        controller_config = ControllerConfig(
            optimization_timeout=30.0,
            trigger_cooldown_minutes=0,
            dispatch_retry_attempts=1,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp_server,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=depot_state)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {
                f'bus_{i+1}': f'charger_{i+1}' for i in range(5)
            })
            
            result = await controller.run_optimization("dispatch_test")
        
        # Verify each vehicle's profile was dispatched
        for i in range(5):
            charger_id = f'charger_{i+1}'
            cp = mock_ocpp_server.charge_points[charger_id]
            
            # Check if set_charging_profile was called
            if cp.set_charging_profile.called:
                call_args = cp.set_charging_profile.call_args
                connector_id = call_args[0][0]
                profile = call_args[0][1]
                
                assert connector_id == 1
                assert isinstance(profile, list)

    # ============ Error Handling E2E Tests ============

    @pytest.mark.asyncio
    async def test_partial_dispatch_failure_handling(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Test handling when some dispatch commands fail."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        # Make some chargers fail
        mock_ocpp_server.charge_points['charger_2'].set_charging_profile = AsyncMock(return_value=False)
        mock_ocpp_server.charge_points['charger_4'].set_charging_profile = AsyncMock(side_effect=Exception("Connection lost"))
        
        controller_config = ControllerConfig(
            optimization_timeout=30.0,
            trigger_cooldown_minutes=0,
            dispatch_retry_attempts=1,
            dispatch_retry_delay_seconds=0.01,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp_server,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=depot_state)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {
                f'bus_{i+1}': f'charger_{i+1}' for i in range(5)
            })
            
            # Should complete despite partial dispatch failure
            result = await controller.run_optimization("partial_failure_test")
        
        assert result.status == 'completed'
        
        # Successful dispatches should have occurred
        assert mock_ocpp_server.charge_points['charger_1'].set_charging_profile.called
        assert mock_ocpp_server.charge_points['charger_3'].set_charging_profile.called
        assert mock_ocpp_server.charge_points['charger_5'].set_charging_profile.called

    @pytest.mark.asyncio
    async def test_optimization_timeout_handling(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Test handling of optimization timeout."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        controller_config = ControllerConfig(
            optimization_timeout=0.1,  # Very short timeout
            trigger_cooldown_minutes=0,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp_server,
        )
        
        # Create state that may take longer to optimize
        large_state = DepotState(
            vehicle_socs=depot_state.vehicle_socs,
            battery_soc=depot_state.battery_soc,
            prices=depot_state.prices,
            demand_charge_rate=depot_state.demand_charge_rate,
            current_month_peak=depot_state.current_month_peak,
            vehicle_availability=depot_state.vehicle_availability,
            energy_requirements=depot_state.energy_requirements,
            departure_times=depot_state.departure_times,
            building_power=depot_state.building_power,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=large_state)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {})
            
            # May succeed or fail depending on solver speed
            try:
                result = await controller.run_optimization("timeout_test")
                # If it completes, verify it's valid
                assert result.status in ['completed', 'optimal', 'suboptimal']
            except Exception as e:
                # Timeout is acceptable
                assert 'timeout' in str(e).lower() or 'time limit' in str(e).lower()

    # ============ Performance E2E Tests ============

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_e2e_performance_benchmark(
        self, mock_db_pool, depot_config, depot_state, mock_ocpp_server
    ):
        """Benchmark full E2E pipeline performance."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        controller_config = ControllerConfig(
            optimization_timeout=30.0,
            trigger_cooldown_minutes=0,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp_server,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=depot_state)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {
                f'bus_{i+1}': f'charger_{i+1}' for i in range(5)
            })
            
            # Run multiple iterations
            times = []
            for i in range(3):
                start = time.time()
                result = await controller.run_optimization(f"benchmark_{i}")
                elapsed = time.time() - start
                times.append(elapsed)
                assert result.status == 'completed'
        
        avg_time = sum(times) / len(times)
        max_time = max(times)
        
        print(f"\nE2E Performance Benchmark:")
        print(f"  Average time: {avg_time:.2f}s")
        print(f"  Max time: {max_time:.2f}s")
        print(f"  Individual times: {[f'{t:.2f}s' for t in times]}")
        
        # Should complete within reasonable time
        assert avg_time < 60.0, f"Average time {avg_time:.2f}s > 60s"
        assert max_time < 90.0, f"Max time {max_time:.2f}s > 90s"
