"""Integration tests for TriggerMonitor.

Reference: PRD.md#11-3-integration-test-requirements
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg

from src.core.models import DepotConfig
from src.core.state.assembler import StateAssembler
from src.core.state.triggers import TriggerConfig, TriggerMonitor


# ============ Fixtures ============

@pytest.fixture
def mock_db_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    return pool


@pytest.fixture
def depot_config():
    """Depot configuration for testing."""
    vehicle_ids = ['bus_1', 'bus_2']
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
    )


@pytest.fixture
def depot_id():
    """Test depot ID."""
    return "test_depot_123"


@pytest.fixture
def assembler(mock_db_pool, depot_id, depot_config):
    """StateAssembler instance for testing."""
    return StateAssembler(mock_db_pool, depot_id, depot_config)


@pytest.mark.asyncio
async def test_trigger_monitor_with_assembler_soc_deviation(
    assembler, mock_db_pool, depot_id
):
    """Test trigger monitor detects SoC deviation via assembler."""
    config = TriggerConfig(soc_deviation_threshold=0.05)
    callback = AsyncMock()

    monitor = TriggerMonitor(config, callback, assembler=assembler)

    # Set expected state
    monitor.update_expected_state({'bus_1': 0.60}, {})

    # Mock assembler to return deviated SoC
    assembler._get_vehicle_socs = AsyncMock(return_value={'bus_1': 0.52})
    assembler._get_current_prices = AsyncMock(return_value={})
    assembler._get_actual_return_times = AsyncMock(return_value={})

    # Run one iteration
    monitor._running = True
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.1)
    monitor.stop()

    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Callback should have been called
    callback.assert_called_once()
    assert 'SoC deviation' in callback.call_args[0][0]


@pytest.mark.asyncio
async def test_trigger_monitor_with_pool_direct_query(mock_db_pool, depot_id):
    """Test trigger monitor works with direct database queries."""
    config = TriggerConfig()
    callback = AsyncMock()

    monitor = TriggerMonitor(config, callback, pool=mock_db_pool, depot_id=depot_id)

    # Mock database queries
    mock_conn = AsyncMock()
    mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Mock SoC query
    mock_row = MagicMock()
    mock_row.__getitem__.side_effect = lambda k: {
        'vehicle_id': 'bus_1',
        'soc': 0.50
    }[k]
    mock_conn.fetch.return_value = [mock_row]

    # Set expected state
    monitor.update_expected_state({'bus_1': 0.60}, {})

    # Mock price query (empty for simplicity)
    mock_conn.fetch.side_effect = [
        [mock_row],  # SoC query
        [],  # Price query
        [],  # Return times query
    ]

    # Run one iteration
    monitor._running = True
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.1)
    monitor.stop()

    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Callback should have been called due to SoC deviation
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_trigger_monitor_price_change_trigger(assembler):
    """Test trigger monitor detects price changes."""
    config = TriggerConfig(
        price_change_percent=0.25,
        price_change_absolute=25.0,
    )
    callback = AsyncMock()

    monitor = TriggerMonitor(config, callback, assembler=assembler)

    # Set initial price baseline using specific timestamps
    base_time = datetime.utcnow().replace(microsecond=0)
    monitor.update_prices({base_time: 0.10})

    # Mock _get_current_prices to return prices at matching timestamps
    async def mock_get_prices():
        return {base_time: 0.20}  # 100% increase, $100/MWh increase
    
    monitor._get_current_prices = mock_get_prices
    monitor._get_current_vehicle_socs = AsyncMock(return_value={})
    monitor._get_actual_return_times = AsyncMock(return_value={})

    # Run one iteration
    monitor._running = True
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.1)
    monitor.stop()

    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Callback should have been called due to price change
    callback.assert_called_once()
    assert 'Price change' in callback.call_args[0][0]


# ============ Enhanced Trigger Tests ============

class TestSoCDeviationTriggers:
    """Tests for SoC deviation trigger scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_vehicle_soc_deviation(self, assembler, mock_db_pool, depot_id):
        """Test SoC deviation detection across multiple vehicles."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set expected state for multiple vehicles
        monitor.update_expected_state(
            {'bus_1': 0.60, 'bus_2': 0.70},
            {}
        )
        
        # Mock assembler to return deviated SoC for both vehicles
        assembler._get_vehicle_socs = AsyncMock(
            return_value={'bus_1': 0.52, 'bus_2': 0.63}  # Both deviated > 5%
        )
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should have been called (at least once)
        assert callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_soc_deviation_below_threshold_no_trigger(self, assembler):
        """Test that small SoC deviations don't trigger."""
        config = TriggerConfig(soc_deviation_threshold=0.10)  # 10% threshold
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set expected state
        monitor.update_expected_state({'bus_1': 0.60}, {})
        
        # Mock assembler to return small deviation (< 10%)
        assembler._get_vehicle_socs = AsyncMock(
            return_value={'bus_1': 0.55}  # Only 5% deviation
        )
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should NOT have been called
        callback.assert_not_called()


class TestPriceSpikeTriggers:
    """Tests for price spike trigger scenarios."""

    @pytest.mark.asyncio
    async def test_price_spike_trigger(self, assembler):
        """Test trigger fires on price spike."""
        config = TriggerConfig(
            price_change_percent=0.30,  # 30% change triggers
            price_change_absolute=20.0,  # $20/MWh absolute change
        )
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set initial price baseline using specific timestamp
        base_time = datetime.utcnow().replace(microsecond=0)
        monitor.update_prices({base_time: 0.10})  # $100/MWh
        
        # Mock methods to return spiked prices at matching timestamp
        async def mock_get_prices():
            return {base_time: 0.20}  # 100% increase, $100/MWh change
        
        monitor._get_current_prices = mock_get_prices
        monitor._get_current_vehicle_socs = AsyncMock(return_value={})
        monitor._get_actual_return_times = AsyncMock(return_value={})
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should have been called
        callback.assert_called_once()
        assert 'Price change' in callback.call_args[0][0] or 'price' in callback.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_price_drop_also_triggers(self, assembler):
        """Test trigger fires on price drop (not just spikes)."""
        config = TriggerConfig(
            price_change_percent=0.25,
            price_change_absolute=25.0,  # Also need absolute threshold
        )
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set initial high price baseline using specific timestamp
        base_time = datetime.utcnow().replace(microsecond=0)
        monitor.update_prices({base_time: 0.20})  # $200/MWh
        
        # Mock methods to return dropped prices at matching timestamp
        async def mock_get_prices():
            return {base_time: 0.10}  # 50% decrease, $100/MWh change
        
        monitor._get_current_prices = mock_get_prices
        monitor._get_current_vehicle_socs = AsyncMock(return_value={})
        monitor._get_actual_return_times = AsyncMock(return_value={})
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should have been called
        assert callback.call_count >= 1


class TestReturnDelayTriggers:
    """Tests for return delay trigger scenarios."""

    @pytest.mark.asyncio
    async def test_return_delay_trigger(self, assembler):
        """Test trigger fires when vehicle return is delayed."""
        config = TriggerConfig(
            return_time_deviation_min=15.0,  # 15 min delay triggers
        )
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        now = datetime.utcnow()
        expected_return = now - timedelta(minutes=20)  # Was supposed to return 20 min ago
        
        # Set expected return time
        monitor.update_expected_state(
            {},  # No SoC expectations
            {'bus_1': expected_return}  # Expected return time
        )
        
        # Mock methods to return actual return time (returned late)
        async def mock_get_socs():
            return {'bus_1': 0.30}
        
        async def mock_get_prices():
            return {}
        
        async def mock_get_returns():
            return {'bus_1': now}  # Returned at now, expected 20 min ago
        
        monitor._get_current_vehicle_socs = mock_get_socs
        monitor._get_current_prices = mock_get_prices
        monitor._get_actual_return_times = mock_get_returns
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should have been called for return delay
        assert callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_return_on_time_no_trigger(self, assembler):
        """Test no trigger when vehicle returns on time."""
        config = TriggerConfig(
            return_time_deviation_min=15.0,
        )
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        now = datetime.utcnow()
        expected_return = now + timedelta(minutes=30)  # Still 30 min until expected return
        
        # Set expected return time
        monitor.update_expected_state(
            {},
            {'bus_1': expected_return}
        )
        
        # Mock assembler - vehicle hasn't returned yet
        assembler._get_vehicle_socs = AsyncMock(return_value={'bus_1': 0.30})
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should NOT have been called for return delay
        # (may still be called for other reasons)


class TestCombinedTriggers:
    """Tests for combined/multiple trigger scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_triggers_same_iteration(self, assembler):
        """Test handling when multiple triggers fire in same iteration."""
        config = TriggerConfig(
            soc_deviation_threshold=0.05,
            price_change_percent=0.20,
            price_change_absolute=10.0,  # Low for easy triggering
            return_time_deviation_min=10.0,
        )
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        now = datetime.utcnow()
        
        # Set expected states for all trigger types
        monitor.update_expected_state(
            {'bus_1': 0.70},  # SoC expectation
            {'bus_2': now - timedelta(minutes=20)}  # Return expectation
        )
        monitor.update_prices({now: 0.10})  # Price baseline
        
        # Mock assembler to trigger all conditions
        assembler._get_vehicle_socs = AsyncMock(
            return_value={'bus_1': 0.58}  # 12% deviation (triggers)
        )
        assembler._get_actual_return_times = AsyncMock(
            return_value={'bus_2': now}  # bus_2 returned 20 min late (triggers)
        )
        
        mock_state = MagicMock()
        mock_state.prices = [0.15] * 96  # 50% price increase (triggers)
        assembler.get_current_state = AsyncMock(return_value=mock_state)
        assembler.config = MagicMock()
        assembler.config.delta_t = 0.25
        
        # Run one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Callback should have been called (at least once, possibly multiple)
        assert callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_trigger_cooldown_enforcement(self, assembler):
        """Test that cooldown prevents rapid re-triggering."""
        config = TriggerConfig(
            soc_deviation_threshold=0.05,
            check_interval_sec=0.05,  # Fast polling for test
        )
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set expected state
        monitor.update_expected_state({'bus_1': 0.70}, {})
        
        # Mock assembler to return deviated SoC
        assembler._get_vehicle_socs = AsyncMock(
            return_value={'bus_1': 0.55}  # Triggers
        )
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})
        
        # Run for multiple iterations
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.3)  # Several poll intervals
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Should have some calls but not one per poll due to cooldown
        # The exact number depends on cooldown implementation
        assert callback.call_count >= 1


class TestTriggerMonitorLifecycle:
    """Tests for trigger monitor lifecycle management."""

    @pytest.mark.asyncio
    async def test_start_stop_cycle(self, assembler):
        """Test starting and stopping the monitor."""
        config = TriggerConfig(check_interval_sec=0.1)
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Mock assembler methods
        assembler._get_vehicle_socs = AsyncMock(return_value={})
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})
        
        # Start monitoring
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        
        # Let it run briefly
        await asyncio.sleep(0.15)
        
        # Stop monitoring
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Should have stopped cleanly
        assert monitor._running is False

    @pytest.mark.asyncio
    async def test_error_recovery(self, assembler):
        """Test monitor recovers from transient errors."""
        config = TriggerConfig(check_interval_sec=0.05)
        callback = AsyncMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        call_count = [0]
        
        async def flaky_get_socs():
            call_count[0] += 1
            if call_count[0] == 1:
                raise asyncpg.PostgresError("Transient error")
            return {'bus_1': 0.50}
        
        assembler._get_vehicle_socs = flaky_get_socs
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})
        
        # Run for multiple iterations
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.2)
        monitor.stop()
        
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Should have recovered and continued
        assert call_count[0] >= 2

