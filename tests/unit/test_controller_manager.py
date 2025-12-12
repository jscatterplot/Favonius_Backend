"""Unit tests for ControllerManager.

Reference: Development plan Step 5.2, PRD.md#11-2-unit-test-requirements
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg

from src.core.controller_manager import ControllerManager
from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig


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
def depot_config():
    """Sample depot configuration."""
    return DepotConfig(
        vehicle_capacities={'bus_1': 324.0, 'bus_2': 324.0},
        charger_power=80.0,
        charger_efficiency=0.95,
        n_chargers=5,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


@pytest.fixture
def controller_config():
    """Controller configuration for testing."""
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=7,
        hourly_optimization_end=23,
        optimization_timeout=30.0,
        trigger_cooldown_minutes=1,
        max_optimization_failures=3,
        dispatch_retry_attempts=2,
        dispatch_retry_delay_seconds=0.1,
        shutdown_timeout_seconds=2.0,  # Short for testing
    )


# ============ Initialization Tests ============

class TestControllerManagerInitialization:
    """Tests for ControllerManager initialization."""

    def test_manager_initialization(self, mock_db_pool, controller_config):
        """Test basic manager initialization."""
        pool, _ = mock_db_pool
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        assert manager.pool == pool
        assert manager.controller_config == controller_config
        assert manager.controllers == {}
        assert manager._running is False

    def test_manager_initialization_with_ocpp(self, mock_db_pool, controller_config):
        """Test manager initialization with OCPP server."""
        pool, _ = mock_db_pool
        mock_ocpp = MagicMock()
        
        manager = ControllerManager(
            pool=pool,
            ocpp_server=mock_ocpp,
            controller_config=controller_config,
        )
        
        assert manager.ocpp_server == mock_ocpp

    def test_manager_initialization_default_config(self, mock_db_pool):
        """Test manager uses default config when not provided."""
        pool, _ = mock_db_pool
        
        with patch.object(ControllerConfig, 'from_env') as mock_from_env:
            mock_from_env.return_value = ControllerConfig()
            
            manager = ControllerManager(pool=pool)
            
            mock_from_env.assert_called_once()


# ============ Start All Controllers Tests ============

class TestStartAllControllers:
    """Tests for starting all controllers."""

    @pytest.mark.asyncio
    async def test_start_all_controllers_from_database(
        self, mock_db_pool, controller_config, depot_config
    ):
        """Test starting controllers for all depots in database."""
        pool, conn = mock_db_pool
        
        depot_ids = [str(uuid4()), str(uuid4())]
        conn.fetch.return_value = [
            {'depot_id': depot_ids[0]},
            {'depot_id': depot_ids[1]},
        ]
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        with patch.object(manager, 'add_controller', new_callable=AsyncMock) as mock_add:
            mock_controller = MagicMock(spec=DepotController)
            mock_add.return_value = mock_controller
            
            await manager.start_all_controllers()
            
            assert manager._running is True
            assert mock_add.call_count == 2

    @pytest.mark.asyncio
    async def test_start_all_controllers_no_depots(self, mock_db_pool, controller_config):
        """Test start when no depots in database."""
        pool, conn = mock_db_pool
        conn.fetch.return_value = []
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        await manager.start_all_controllers()
        
        assert manager._running is True
        assert len(manager.controllers) == 0

    @pytest.mark.asyncio
    async def test_start_all_controllers_already_running(
        self, mock_db_pool, controller_config
    ):
        """Test start when manager already running."""
        pool, _ = mock_db_pool
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        manager._running = True
        
        # Should return early without error
        await manager.start_all_controllers()

    @pytest.mark.asyncio
    async def test_start_all_controllers_handles_individual_failure(
        self, mock_db_pool, controller_config
    ):
        """Test start continues when individual controller fails."""
        pool, conn = mock_db_pool
        
        depot_ids = [str(uuid4()), str(uuid4())]
        conn.fetch.return_value = [
            {'depot_id': depot_ids[0]},
            {'depot_id': depot_ids[1]},
        ]
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        call_count = [0]
        
        async def mock_add(depot_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("Config error")
            return MagicMock(spec=DepotController)
        
        manager.add_controller = mock_add
        
        await manager.start_all_controllers()
        
        # Should still have started second controller
        assert call_count[0] == 2


# ============ Stop All Controllers Tests ============

class TestStopAllControllers:
    """Tests for stopping all controllers."""

    @pytest.mark.asyncio
    async def test_stop_all_controllers_graceful(self, mock_db_pool, controller_config):
        """Test graceful stop of all controllers."""
        pool, _ = mock_db_pool
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        manager._running = True
        
        # Add mock controllers
        mock_controller1 = AsyncMock(spec=DepotController)
        mock_controller2 = AsyncMock(spec=DepotController)
        
        manager.controllers = {
            'depot_1': mock_controller1,
            'depot_2': mock_controller2,
        }
        
        await manager.stop_all_controllers()
        
        assert manager._running is False
        assert len(manager.controllers) == 0
        mock_controller1.stop_async.assert_called_once()
        mock_controller2.stop_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_all_controllers_not_running(self, mock_db_pool, controller_config):
        """Test stop when manager not running."""
        pool, _ = mock_db_pool
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        manager._running = False
        
        # Should return early without error
        await manager.stop_all_controllers()

    @pytest.mark.asyncio
    async def test_stop_all_controllers_timeout(self, mock_db_pool, controller_config):
        """Test stop handles timeout gracefully."""
        pool, _ = mock_db_pool
        
        controller_config.shutdown_timeout_seconds = 0.1
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        manager._running = True
        
        # Create a slow mock controller
        async def slow_stop(*args, **kwargs):
            await asyncio.sleep(10)
        
        mock_controller = MagicMock(spec=DepotController)
        mock_controller.stop_async = slow_stop
        
        manager.controllers = {'depot_1': mock_controller}
        
        # Should complete without raising
        await manager.stop_all_controllers()
        
        assert manager._running is False


# ============ Add Controller Tests ============

class TestAddController:
    """Tests for adding controllers."""

    @pytest.mark.asyncio
    async def test_add_controller_creates_new(
        self, mock_db_pool, controller_config, depot_config
    ):
        """Test adding a new controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        with patch(
            'src.core.controller_manager.StateAssembler.load_depot_config',
            new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {})
            
            controller = await manager.add_controller(depot_id)
            
            assert depot_id in manager.controllers
            assert isinstance(controller, DepotController)

    @pytest.mark.asyncio
    async def test_add_controller_returns_existing(
        self, mock_db_pool, controller_config
    ):
        """Test adding controller returns existing one."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        existing_controller = MagicMock(spec=DepotController)
        manager.controllers[depot_id] = existing_controller
        
        returned = await manager.add_controller(depot_id)
        
        assert returned is existing_controller

    @pytest.mark.asyncio
    async def test_add_controller_starts_when_running(
        self, mock_db_pool, controller_config, depot_config
    ):
        """Test add_controller starts the controller when manager is running."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        manager._running = True
        
        with patch(
            'src.core.controller_manager.StateAssembler.load_depot_config',
            new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {})
            
            await manager.add_controller(depot_id)
            
            assert depot_id in manager._controller_tasks

    @pytest.mark.asyncio
    async def test_add_controller_with_uuid(
        self, mock_db_pool, controller_config, depot_config
    ):
        """Test adding controller with UUID object."""
        pool, _ = mock_db_pool
        depot_uuid = uuid4()
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        with patch(
            'src.core.controller_manager.StateAssembler.load_depot_config',
            new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {})
            
            await manager.add_controller(depot_uuid)
            
            assert str(depot_uuid) in manager.controllers

    @pytest.mark.asyncio
    async def test_add_controller_raises_on_config_error(
        self, mock_db_pool, controller_config
    ):
        """Test add_controller raises on configuration error."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        with patch(
            'src.core.controller_manager.StateAssembler.load_depot_config',
            new_callable=AsyncMock
        ) as mock_load:
            mock_load.side_effect = ValueError("Depot not found")
            
            with pytest.raises(ValueError, match="Depot not found"):
                await manager.add_controller(depot_id)


# ============ Remove Controller Tests ============

class TestRemoveController:
    """Tests for removing controllers."""

    @pytest.mark.asyncio
    async def test_remove_controller_stops_and_removes(
        self, mock_db_pool, controller_config
    ):
        """Test remove_controller stops and removes controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = AsyncMock(spec=DepotController)
        manager.controllers[depot_id] = mock_controller
        
        await manager.remove_controller(depot_id)
        
        assert depot_id not in manager.controllers
        mock_controller.stop_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_controller_cancels_task(
        self, mock_db_pool, controller_config
    ):
        """Test remove_controller cancels controller task."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = AsyncMock(spec=DepotController)
        manager.controllers[depot_id] = mock_controller
        
        # Create a mock task
        async def long_running():
            await asyncio.sleep(100)
        
        task = asyncio.create_task(long_running())
        manager._controller_tasks[depot_id] = task
        
        await manager.remove_controller(depot_id)
        
        assert depot_id not in manager._controller_tasks
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_remove_controller_not_found(
        self, mock_db_pool, controller_config
    ):
        """Test remove_controller handles non-existent controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        # Should not raise
        await manager.remove_controller(depot_id)


# ============ Get Controller Tests ============

class TestGetController:
    """Tests for getting controllers."""

    def test_get_controller_existing(self, mock_db_pool, controller_config):
        """Test get_controller returns existing controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = MagicMock(spec=DepotController)
        manager.controllers[depot_id] = mock_controller
        
        result = manager.get_controller(depot_id)
        
        assert result is mock_controller

    def test_get_controller_not_found(self, mock_db_pool, controller_config):
        """Test get_controller returns None for non-existent."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        result = manager.get_controller(depot_id)
        
        assert result is None

    def test_get_controller_with_uuid(self, mock_db_pool, controller_config):
        """Test get_controller works with UUID object."""
        pool, _ = mock_db_pool
        depot_uuid = uuid4()
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = MagicMock(spec=DepotController)
        manager.controllers[str(depot_uuid)] = mock_controller
        
        result = manager.get_controller(depot_uuid)
        
        assert result is mock_controller


# ============ Get Or Create Controller Tests ============

class TestGetOrCreateController:
    """Tests for get_or_create_controller."""

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(
        self, mock_db_pool, controller_config
    ):
        """Test get_or_create returns existing controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = MagicMock(spec=DepotController)
        manager.controllers[depot_id] = mock_controller
        
        result = await manager.get_or_create_controller(depot_id)
        
        assert result is mock_controller

    @pytest.mark.asyncio
    async def test_get_or_create_creates_new(
        self, mock_db_pool, controller_config, depot_config
    ):
        """Test get_or_create creates controller if not exists."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        with patch(
            'src.core.controller_manager.StateAssembler.load_depot_config',
            new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {})
            
            result = await manager.get_or_create_controller(depot_id)
            
            assert result is not None
            assert depot_id in manager.controllers


# ============ List Controllers Tests ============

class TestListControllers:
    """Tests for listing controllers."""

    def test_list_controllers_empty(self, mock_db_pool, controller_config):
        """Test list_controllers with no controllers."""
        pool, _ = mock_db_pool
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        result = manager.list_controllers()
        
        assert result == []

    def test_list_controllers_returns_depot_ids(self, mock_db_pool, controller_config):
        """Test list_controllers returns depot IDs."""
        pool, _ = mock_db_pool
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        depot_ids = [str(uuid4()), str(uuid4())]
        manager.controllers = {
            depot_ids[0]: MagicMock(spec=DepotController),
            depot_ids[1]: MagicMock(spec=DepotController),
        }
        
        result = manager.list_controllers()
        
        assert set(result) == set(depot_ids)


# ============ Health Check Tests ============

class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_healthy_controller(
        self, mock_db_pool, controller_config
    ):
        """Test health check for healthy controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = MagicMock(spec=DepotController)
        mock_controller._running = True
        mock_controller.last_run_time = datetime.utcnow()
        mock_controller._optimization_failures = 0
        mock_controller._circuit_breaker_open = False
        
        manager.controllers[depot_id] = mock_controller
        
        health = await manager.health_check()
        
        assert depot_id in health
        assert health[depot_id]['status'] == 'healthy'
        assert health[depot_id]['running'] is True

    @pytest.mark.asyncio
    async def test_health_check_degraded_controller(
        self, mock_db_pool, controller_config
    ):
        """Test health check for degraded controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = MagicMock(spec=DepotController)
        mock_controller._running = True
        mock_controller.last_run_time = datetime.utcnow()
        mock_controller._optimization_failures = 2
        mock_controller._circuit_breaker_open = True
        
        manager.controllers[depot_id] = mock_controller
        
        health = await manager.health_check()
        
        assert health[depot_id]['status'] == 'degraded'
        assert health[depot_id]['circuit_breaker_open'] is True

    @pytest.mark.asyncio
    async def test_health_check_error_handling(
        self, mock_db_pool, controller_config
    ):
        """Test health check handles controller errors."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )
        
        mock_controller = MagicMock(spec=DepotController)
        # Make accessing _running raise an error
        type(mock_controller)._running = property(
            lambda self: (_ for _ in ()).throw(Exception("Access error"))
        )
        
        manager.controllers[depot_id] = mock_controller
        
        health = await manager.health_check()
        
        assert health[depot_id]['status'] == 'error'
        assert 'error' in health[depot_id]


# ============ Controller Config Tests ============

class TestControllerConfig:
    """Tests for ControllerConfig dataclass."""

    def test_controller_config_defaults(self):
        """Test ControllerConfig default values."""
        config = ControllerConfig()
        
        assert config.optimization_horizon_hours == 24
        assert config.hourly_optimization_start == 7
        assert config.hourly_optimization_end == 23
        assert config.optimization_timeout == 30.0
        assert config.trigger_cooldown_minutes == 5
        assert config.max_optimization_failures == 3
        assert config.dispatch_retry_attempts == 3

    def test_controller_config_from_env(self):
        """Test ControllerConfig.from_env()."""
        import os
        
        # Set environment variables
        env_vars = {
            'FAVONIUS_OPTIMIZATION_HORIZON_HOURS': '12',
            'FAVONIUS_HOURLY_OPT_START': '8',
            'FAVONIUS_HOURLY_OPT_END': '20',
            'FAVONIUS_OPTIMIZATION_TIMEOUT': '60.0',
        }
        
        with patch.dict(os.environ, env_vars):
            config = ControllerConfig.from_env()
            
            assert config.optimization_horizon_hours == 12
            assert config.hourly_optimization_start == 8
            assert config.hourly_optimization_end == 20
            assert config.optimization_timeout == 60.0

    def test_controller_config_validate_valid(self):
        """Test ControllerConfig validation with valid config."""
        config = ControllerConfig(
            optimization_horizon_hours=24,
            hourly_optimization_start=7,
            hourly_optimization_end=23,
            optimization_timeout=30.0,
            trigger_cooldown_minutes=5,
            max_optimization_failures=3,
            dispatch_retry_attempts=3,
        )
        
        # Should not raise
        config.validate()

    def test_controller_config_validate_invalid_horizon(self):
        """Test validation rejects invalid horizon."""
        config = ControllerConfig(optimization_horizon_hours=100)
        
        with pytest.raises(ValueError, match="optimization_horizon_hours"):
            config.validate()

    def test_controller_config_validate_invalid_hours(self):
        """Test validation rejects invalid start/end hours."""
        config = ControllerConfig(
            hourly_optimization_start=20,
            hourly_optimization_end=10,
        )
        
        with pytest.raises(ValueError, match="hourly_optimization_start"):
            config.validate()

    def test_controller_config_validate_invalid_timeout(self):
        """Test validation rejects invalid timeout."""
        config = ControllerConfig(optimization_timeout=0)
        
        with pytest.raises(ValueError, match="optimization_timeout"):
            config.validate()

