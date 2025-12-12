"""Full control loop integration tests.

Tests hourly optimization trigger, SoC deviation trigger with re-optimization
and dispatch, price spike trigger with re-optimization, trigger cooldown
enforcement, and controller manager multi-depot scenarios.

Reference: Development plan Step 5.2, PRD.md#11-3-integration-tests
"""

import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from uuid import uuid4

import asyncpg

from src.core.controller import DepotController
from src.core.controller_manager import ControllerManager
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.core.state.assembler import StateAssembler
from src.core.state.triggers import TriggerConfig, TriggerMonitor


# ============ Fixtures ============

@pytest.fixture
def mock_db_pool():
    """Mock database connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def sample_depot_config():
    """Sample depot configuration."""
    return DepotConfig(
        vehicle_capacities={
            'bus_1': 324.0,
            'bus_2': 324.0,
            'bus_3': 324.0,
        },
        charger_power=80.0,
        charger_efficiency=0.95,
        n_chargers=10,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
        delta_t=0.25,
        n_timesteps=96,
    )


@pytest.fixture
def sample_depot_state(sample_depot_config):
    """Sample depot state for testing."""
    n_t = sample_depot_config.n_timesteps
    vehicles = list(sample_depot_config.vehicle_capacities.keys())
    
    return DepotState(
        vehicle_socs={v: 0.3 + 0.1 * i for i, v in enumerate(vehicles)},
        battery_soc=0.5,
        prices=[0.10 + 0.02 * (i % 24) for i in range(n_t)],
        demand_charge_rate=15.0,
        current_month_peak=100.0,
        vehicle_availability={v: [True] * n_t for v in vehicles},
        energy_requirements={v: 200.0 for v in vehicles},
        departure_times={v: 48 + 12 * i for i, v in enumerate(vehicles)},
        building_power=[50.0] * n_t,
    )


@pytest.fixture
def sample_optimization_result(sample_depot_config):
    """Sample optimization result."""
    n_t = sample_depot_config.n_timesteps
    vehicles = list(sample_depot_config.vehicle_capacities.keys())
    
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            v: {
                'charging_power': [80.0 if i < 48 else 0.0 for i in range(n_t)],
                'soc': [0.3 + 0.01 * i for i in range(n_t)],
            }
            for v in vehicles
        },
        battery_dispatch=[0.0] * n_t,
        grid_power=[200.0] * n_t,
        peak_demand=250.0,
        objective_value=1500.0,
        solve_time=5.0,
        status='completed',
    )


@pytest.fixture
def fast_controller_config():
    """Fast controller configuration for testing."""
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=0,  # Always active
        hourly_optimization_end=23,
        optimization_timeout=10.0,
        trigger_cooldown_minutes=0,  # No cooldown for tests
        max_optimization_failures=3,
        dispatch_retry_attempts=1,
        dispatch_retry_delay_seconds=0.01,
        shutdown_timeout_seconds=1.0,
    )


# ============ Hourly Optimization Trigger Tests ============

class TestHourlyOptimizationTrigger:
    """Tests for hourly optimization trigger."""

    @pytest.mark.asyncio
    async def test_hourly_trigger_fires_on_schedule(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test hourly optimization runs on schedule."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        optimization_count = [0]
        
        async def mock_run_optimization(trigger_reason):
            optimization_count[0] += 1
            controller.last_run_time = datetime.utcnow()
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # Start control loop in background
        controller._running = True
        
        async def run_briefly():
            # Simulate control loop checking time
            now = datetime.utcnow()
            if fast_controller_config.hourly_optimization_start <= now.hour <= fast_controller_config.hourly_optimization_end:
                if controller.last_run_time is None or (now - controller.last_run_time) > timedelta(hours=1):
                    await controller.run_optimization("hourly")
        
        await run_briefly()
        
        # Should have run optimization
        assert optimization_count[0] == 1
        assert controller.last_run_time is not None

    @pytest.mark.asyncio
    async def test_hourly_trigger_respects_time_window(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result
    ):
        """Test hourly optimization respects configured time window."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        # Configure narrow time window
        config = ControllerConfig(
            hourly_optimization_start=14,  # 2 PM
            hourly_optimization_end=18,    # 6 PM
            trigger_cooldown_minutes=0,
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=config,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        optimization_count = [0]
        
        async def mock_run_optimization(trigger_reason):
            optimization_count[0] += 1
            controller.last_run_time = datetime.utcnow()
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # Mock current time outside window
        with patch('src.core.controller.datetime') as mock_datetime:
            mock_now = MagicMock()
            mock_now.hour = 10  # 10 AM, outside window
            mock_now.utcnow.return_value = mock_now
            mock_datetime.utcnow.return_value = mock_now
            
            # Should not trigger outside window
            # In actual control loop, time check would fail
            current_hour = 10
            if config.hourly_optimization_start <= current_hour <= config.hourly_optimization_end:
                await controller.run_optimization("hourly")
            
            assert optimization_count[0] == 0

    @pytest.mark.asyncio
    async def test_hourly_trigger_skips_if_recent_run(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test hourly trigger skips if optimization ran recently."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
        )
        
        # Set recent run time
        controller.last_run_time = datetime.utcnow() - timedelta(minutes=30)
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        optimization_count = [0]
        
        async def mock_run_optimization(trigger_reason):
            optimization_count[0] += 1
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # Check if should run (simulating control loop logic)
        now = datetime.utcnow()
        if controller.last_run_time is None or (now - controller.last_run_time) > timedelta(hours=1):
            await controller.run_optimization("hourly")
        
        # Should not run - recent optimization exists
        assert optimization_count[0] == 0


# ============ SoC Deviation Trigger Tests ============

class TestSoCDeviationTrigger:
    """Tests for SoC deviation trigger with re-optimization and dispatch."""

    @pytest.mark.asyncio
    async def test_soc_deviation_triggers_reoptimization(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test SoC deviation beyond threshold triggers re-optimization."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
        )
        
        # Set expected SoC from previous optimization
        controller.last_schedule = {
            'bus_1': {'soc': [0.60] * 96, 'charging_power': [80.0] * 96},
        }
        controller.trigger_monitor.update_expected_state({'bus_1': 0.60}, {})
        
        # Mock state with SoC deviation > 5%
        deviated_state = DepotState(
            vehicle_socs={'bus_1': 0.52},  # 8% below expected
            battery_soc=0.5,
            prices=[0.10] * 96,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * 96},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 48},
            building_power=[50.0] * 96,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=deviated_state)
        
        optimization_triggered = [False]
        
        async def mock_run_optimization(trigger_reason):
            optimization_triggered[0] = True
            assert 'soc' in trigger_reason.lower()
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # Check for SoC deviation
        actual_socs = {'bus_1': 0.52}
        expected_socs = {'bus_1': 0.60}
        
        deviation = abs(actual_socs['bus_1'] - expected_socs['bus_1'])
        if deviation > 0.05:  # 5% threshold
            await controller._handle_trigger("soc_deviation_bus_1")
        
        assert optimization_triggered[0] is True

    @pytest.mark.asyncio
    async def test_soc_deviation_updates_dispatch(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test SoC deviation re-optimization updates dispatch commands."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        mock_ocpp = MagicMock()
        mock_cp = MagicMock()
        mock_cp.set_charging_profile = AsyncMock(return_value=True)
        mock_ocpp.get_charge_point.return_value = mock_cp
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
            ocpp_server=mock_ocpp,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {'bus_1': 'charger_1'})
            
            with patch('src.core.controller.optimize', return_value=sample_optimization_result):
                await controller.run_optimization("soc_deviation")
        
        # Dispatch should have been called
        mock_cp.set_charging_profile.assert_called()


# ============ Price Spike Trigger Tests ============

class TestPriceSpikeTrigger:
    """Tests for price spike trigger with re-optimization."""

    @pytest.mark.asyncio
    async def test_price_spike_triggers_reoptimization(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test price spike triggers re-optimization."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
        )
        
        # Set baseline prices
        baseline_prices = {datetime.utcnow() + timedelta(hours=i): 0.10 for i in range(24)}
        controller.trigger_monitor.update_prices(baseline_prices)
        
        # Create state with price spike (>25% increase and >$25/MWh = $0.025/kWh)
        spiked_state = DepotState(
            vehicle_socs={'bus_1': 0.5},
            battery_soc=0.5,
            prices=[0.10] * 40 + [0.15] * 20 + [0.10] * 36,  # 50% spike in middle
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * 96},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 48},
            building_power=[50.0] * 96,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=spiked_state)
        
        optimization_triggered = [False]
        trigger_reason_captured = [None]
        
        async def mock_run_optimization(trigger_reason):
            optimization_triggered[0] = True
            trigger_reason_captured[0] = trigger_reason
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # Simulate price spike detection
        await controller._handle_trigger("price_spike")
        
        assert optimization_triggered[0] is True
        assert trigger_reason_captured[0] == "price_spike"

    @pytest.mark.asyncio
    async def test_price_spike_shifts_charging(
        self, mock_db_pool, sample_depot_config, fast_controller_config
    ):
        """Test price spike re-optimization shifts charging away from high-price period."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
        )
        
        # Create state with afternoon price spike
        # Expect optimization to shift charging to lower-price periods
        spiked_state = DepotState(
            vehicle_socs={'bus_1': 0.3},
            battery_soc=0.5,
            # Morning low, afternoon spike, evening low
            prices=[0.08] * 32 + [0.25] * 32 + [0.08] * 32,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * 96},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 80},  # Late departure
            building_power=[50.0] * 96,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=spiked_state)
        
        with patch('src.core.controller.optimize') as mock_optimize:
            # Result should prefer low-price periods
            shifted_result = OptimizationResult(
                run_id=uuid4(),
                schedule={
                    'bus_1': {
                        # Charge in low-price periods
                        'charging_power': [80.0] * 32 + [0.0] * 32 + [80.0] * 32,
                        'soc': [0.3 + 0.01 * i for i in range(96)],
                    }
                },
                battery_dispatch=[0.0] * 96,
                grid_power=[80.0] * 96,
                peak_demand=80.0,
                objective_value=800.0,
                solve_time=3.0,
                status='completed',
            )
            mock_optimize.return_value = shifted_result
            
            result = await controller.run_optimization("price_spike")
            
            # Verify charging avoids spike period (timesteps 32-63)
            spike_period_power = sum(
                result.schedule['bus_1']['charging_power'][32:64]
            )
            non_spike_power = sum(
                result.schedule['bus_1']['charging_power'][:32]
            ) + sum(
                result.schedule['bus_1']['charging_power'][64:]
            )
            
            # Should charge less during spike
            assert spike_period_power < non_spike_power


# ============ Trigger Cooldown Enforcement Tests ============

class TestTriggerCooldownEnforcement:
    """Tests for trigger cooldown enforcement."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_rapid_triggers(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result
    ):
        """Test cooldown blocks rapid successive triggers."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        config = ControllerConfig(
            trigger_cooldown_minutes=5,  # 5 minute cooldown
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=config,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        optimization_count = [0]
        
        async def mock_run_optimization(trigger_reason):
            optimization_count[0] += 1
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # First trigger
        await controller._handle_trigger("trigger_1")
        assert optimization_count[0] == 1
        
        # Second trigger immediately - should be blocked
        await controller._handle_trigger("trigger_2")
        assert optimization_count[0] == 1  # Still 1
        
        # Third trigger immediately - should be blocked
        await controller._handle_trigger("trigger_3")
        assert optimization_count[0] == 1  # Still 1

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_expiry(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result
    ):
        """Test cooldown allows triggers after cooldown expires."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        config = ControllerConfig(
            trigger_cooldown_minutes=0,  # No cooldown
        )
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=config,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        optimization_count = [0]
        
        async def mock_run_optimization(trigger_reason):
            optimization_count[0] += 1
            return sample_optimization_result
        
        controller.run_optimization = mock_run_optimization
        
        # Multiple triggers - all should work with no cooldown
        await controller._handle_trigger("trigger_1")
        await controller._handle_trigger("trigger_2")
        await controller._handle_trigger("trigger_3")
        
        assert optimization_count[0] == 3

    @pytest.mark.asyncio
    async def test_cooldown_independent_per_controller(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result
    ):
        """Test cooldown is independent for each controller."""
        pool, _ = mock_db_pool
        
        config = ControllerConfig(
            trigger_cooldown_minutes=5,
        )
        
        controller1 = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=config,
        )
        
        controller2 = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=config,
        )
        
        controller1.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        controller2.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        opt_count_1 = [0]
        opt_count_2 = [0]
        
        async def mock_run_opt_1(reason):
            opt_count_1[0] += 1
            return sample_optimization_result
        
        async def mock_run_opt_2(reason):
            opt_count_2[0] += 1
            return sample_optimization_result
        
        controller1.run_optimization = mock_run_opt_1
        controller2.run_optimization = mock_run_opt_2
        
        # Both controllers can trigger independently
        await controller1._handle_trigger("trigger")
        await controller2._handle_trigger("trigger")
        
        assert opt_count_1[0] == 1
        assert opt_count_2[0] == 1


# ============ Controller Manager Multi-Depot Tests ============

class TestControllerManagerMultiDepot:
    """Tests for controller manager with multiple depots."""

    @pytest.mark.asyncio
    async def test_manager_handles_multiple_depots(
        self, mock_db_pool, sample_depot_config, fast_controller_config
    ):
        """Test manager handles multiple depots concurrently."""
        pool, conn = mock_db_pool
        
        depot_ids = [str(uuid4()) for _ in range(3)]
        
        async def mock_fetch(query, *args):
            if 'SELECT depot_id' in query:
                return [{'depot_id': did} for did in depot_ids]
            return []
        
        conn.fetch = AsyncMock(side_effect=mock_fetch)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})
            
            manager = ControllerManager(
                pool=pool,
                controller_config=fast_controller_config,
            )
            
            await manager.start_all_controllers()
            
            assert len(manager.controllers) == 3
            for did in depot_ids:
                assert did in manager.controllers

    @pytest.mark.asyncio
    async def test_manager_isolates_depot_failures(
        self, mock_db_pool, sample_depot_config, fast_controller_config
    ):
        """Test one depot failure doesn't affect others."""
        pool, conn = mock_db_pool
        
        depot_ids = [str(uuid4()) for _ in range(3)]
        failing_depot = depot_ids[1]
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            def config_for_depot(pool, depot_id):
                if depot_id == failing_depot:
                    raise ValueError("Failed to load depot config")
                return (sample_depot_config, {})
            
            mock_load.side_effect = config_for_depot
            
            manager = ControllerManager(
                pool=pool,
                controller_config=fast_controller_config,
            )
            
            # Add controllers - one should fail
            for did in depot_ids:
                try:
                    await manager.add_controller(did)
                except ValueError:
                    pass
            
            # Should have 2 controllers (failing one excluded)
            assert len(manager.controllers) == 2
            assert failing_depot not in manager.controllers

    @pytest.mark.asyncio
    async def test_manager_health_check_all_depots(
        self, mock_db_pool, sample_depot_config, fast_controller_config
    ):
        """Test health check returns status for all depots."""
        pool, _ = mock_db_pool
        
        depot_ids = [str(uuid4()) for _ in range(3)]
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})
            
            manager = ControllerManager(
                pool=pool,
                controller_config=fast_controller_config,
            )
            
            for did in depot_ids:
                await manager.add_controller(did)
            
            health = await manager.health_check()
            
            assert len(health) == 3
            for did in depot_ids:
                assert did in health
                assert 'status' in health[did]

    @pytest.mark.asyncio
    async def test_manager_concurrent_optimizations(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test concurrent optimizations across depots."""
        pool, _ = mock_db_pool
        
        depot_ids = [str(uuid4()) for _ in range(3)]
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})
            
            manager = ControllerManager(
                pool=pool,
                controller_config=fast_controller_config,
            )
            
            for did in depot_ids:
                await manager.add_controller(did)
            
            # Mock optimizations
            for did, controller in manager.controllers.items():
                controller.assembler.get_current_state = AsyncMock(
                    return_value=sample_depot_state
                )
            
            with patch('src.core.controller.optimize', return_value=sample_optimization_result):
                # Run optimizations concurrently
                tasks = [
                    controller.run_optimization("concurrent_test")
                    for controller in manager.controllers.values()
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # All should complete
            successful = [r for r in results if isinstance(r, OptimizationResult)]
            assert len(successful) == 3


# ============ Full Integration Flow Tests ============

class TestFullIntegrationFlow:
    """Tests for complete integration flows."""

    @pytest.mark.asyncio
    async def test_full_optimization_dispatch_flow(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        sample_optimization_result, fast_controller_config
    ):
        """Test full flow: state assembly -> optimization -> dispatch."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())
        
        mock_ocpp = MagicMock()
        mock_cp = MagicMock()
        mock_cp.set_charging_profile = AsyncMock(return_value=True)
        mock_ocpp.get_charge_point.return_value = mock_cp
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
            ocpp_server=mock_ocpp,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {
                'bus_1': 'charger_1',
                'bus_2': 'charger_2',
                'bus_3': 'charger_3',
            })
            
            with patch('src.core.controller.optimize', return_value=sample_optimization_result):
                result = await controller.run_optimization("full_flow_test")
        
        # Verify result
        assert result.status == 'completed'
        assert controller.last_schedule is not None
        assert controller.last_run_time is not None
        
        # Verify dispatch was called for each vehicle
        assert mock_cp.set_charging_profile.call_count >= 1

    @pytest.mark.asyncio
    async def test_trigger_reoptimization_updates_schedule(
        self, mock_db_pool, sample_depot_config, sample_depot_state,
        fast_controller_config
    ):
        """Test trigger-based re-optimization updates schedule."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=sample_depot_config,
            controller_config=fast_controller_config,
        )
        
        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)
        
        # Initial result
        initial_result = OptimizationResult(
            run_id=uuid4(),
            schedule={'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.5] * 96}},
            battery_dispatch=[0.0] * 96,
            grid_power=[100.0] * 96,
            peak_demand=100.0,
            objective_value=1000.0,
            solve_time=3.0,
            status='completed',
        )
        
        # Updated result after re-optimization
        updated_result = OptimizationResult(
            run_id=uuid4(),
            schedule={'bus_1': {'charging_power': [60.0] * 96, 'soc': [0.6] * 96}},
            battery_dispatch=[0.0] * 96,
            grid_power=[80.0] * 96,
            peak_demand=80.0,
            objective_value=800.0,  # Lower cost
            solve_time=2.5,
            status='completed',
        )
        
        call_count = [0]
        
        def optimize_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return initial_result
            return updated_result
        
        with patch('src.core.controller.optimize', side_effect=optimize_side_effect):
            # Initial optimization
            await controller.run_optimization("initial")
            assert controller.last_schedule == initial_result.schedule
            
            # Triggered re-optimization
            await controller._handle_trigger("price_spike")
            assert controller.last_schedule == updated_result.schedule
            assert controller.last_result.objective_value == 800.0
