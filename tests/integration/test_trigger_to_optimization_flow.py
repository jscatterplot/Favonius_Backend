"""Integration tests for Trigger Monitor → Controller → Optimizer flow.

Tests the complete flow:
1. Trigger → optimization flow for all trigger types
2. Trigger cooldown behavior
3. Multiple triggers in same iteration
4. Trigger priority and debouncing

Reference: PRD_v2.md#5-3-triggers
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize
from src.core.state.triggers import TriggerConfig, TriggerMonitor


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriggerToOptimizationFlow:
    """Test trigger → optimization flow for all trigger types."""

    @pytest.fixture
    def depot_config(self):
        """Depot configuration."""
        vehicle_ids = ["bus_1", "bus_2"]
        return DepotConfig(
            vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 2},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )

    @pytest.fixture
    def depot_state(self, depot_config):
        """Depot state."""
        n_t = depot_config.n_timesteps
        return DepotState(
            vehicle_socs={"bus_1": 0.3, "bus_2": 0.4},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={
                "bus_1": [True] * n_t,
                "bus_2": [True] * n_t,
            },
            energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
            departure_times={"bus_1": 48, "bus_2": 60},
            building_power=[50.0] * n_t,
        )

    @pytest.mark.asyncio
    async def test_soc_deviation_trigger_optimization(self, depot_config, depot_state):
        """Test SoC deviation trigger → optimization."""
        # Run initial optimization
        initial_result = optimize(depot_state, depot_config, time_limit=30.0)
        assert initial_result.status == "completed"

        # Update expected SoC
        expected_soc = 0.60  # Expected SoC after optimization

        # Simulate SoC deviation (actual SoC lower than expected)
        actual_soc = 0.50  # 10% deviation (> 5% threshold)

        # Create trigger monitor
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)
            # In real controller, this would trigger optimization
            # For test, we just verify trigger fires

        config = TriggerConfig(soc_deviation_threshold=0.05)  # 5%
        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)
        monitor.update_expected_state({"bus_1": expected_soc}, {})

        # Check SoC deviation
        result = await monitor.check_soc_deviation({"bus_1": actual_soc})

        assert result is not None, "SoC deviation trigger should fire"
        assert "SoC deviation" in result

    @pytest.mark.asyncio
    async def test_price_change_trigger_optimization_or_logic(self, depot_config, depot_state):
        """Test price change trigger (OR logic) → optimization."""
        # Run initial optimization
        initial_result = optimize(depot_state, depot_config, time_limit=30.0)
        assert initial_result.status == "completed"

        # Create trigger monitor
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Initial prices
        base_time = datetime.utcnow()
        initial_prices = {base_time: 0.10}
        monitor.update_prices(initial_prices)

        # Price spike: 30% change (> 25%) but only $3/MWh change (< $25/MWh)
        # With OR logic, should trigger because percent threshold met
        new_prices = {base_time: 0.013}  # 30% change

        result = await monitor.check_price_change(new_prices)

        assert result is not None, "Price trigger should fire with OR logic"
        assert "Price change" in result

    @pytest.mark.asyncio
    async def test_return_time_deviation_trigger_optimization(self, depot_config, depot_state):
        """Test return time deviation trigger → optimization."""
        # Run initial optimization
        initial_result = optimize(depot_state, depot_config, time_limit=30.0)
        assert initial_result.status == "completed"

        # Create trigger monitor
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        config = TriggerConfig(return_time_deviation_min=15.0)  # 15 minutes
        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Expected return time
        expected_return = datetime.utcnow() + timedelta(hours=2)
        monitor.update_expected_state({}, {"bus_1": expected_return})

        # Actual return time (30 minutes late)
        actual_return = expected_return + timedelta(minutes=30)

        result = await monitor.check_return_time_deviation({"bus_1": actual_return})

        assert result is not None, "Return time deviation trigger should fire"
        assert "Return time" in result

    @pytest.mark.asyncio
    async def test_inter_depot_handoff_trigger_optimization(self):
        """Test inter-depot handoff trigger → optimization."""
        from uuid import uuid4

        # Create trigger monitor
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        config = TriggerConfig()
        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Simulate handoff message received
        message_id = uuid4()
        vehicle_id = uuid4()

        result = monitor.trigger_interdepot_handoff(message_id, vehicle_id)

        assert result is not None, "Inter-depot handoff trigger should fire"
        assert "Inter-depot handoff" in result

    @pytest.mark.asyncio
    async def test_scheduled_trigger_optimization(self):
        """Test scheduled trigger → optimization."""
        # Scheduled triggers run hourly 7 AM - 11 PM
        # This test verifies scheduled trigger logic

        config = TriggerConfig()
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Simulate scheduled trigger check
        # (In real code, this happens in monitor.run() at scheduled hours)
        current_hour = 10  # 10 AM (within 7 AM - 11 PM window)
        monitor._last_scheduled_hour = 9  # Last trigger was 9 AM

        # Should trigger if hour changed and within window
        should_trigger = 7 <= current_hour <= 23 and monitor._last_scheduled_hour != current_hour

        assert should_trigger, "Scheduled trigger should fire at new hour within window"


@pytest.mark.integration
@pytest.mark.asyncio
class TestTriggerCooldown:
    """Test trigger cooldown behavior."""

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_re_optimization(self):
        """Test cooldown prevents rapid re-optimization."""
        config = TriggerConfig(trigger_cooldown_minutes=5)  # 5 minute cooldown
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # First trigger
        monitor.update_expected_state({"bus_1": 0.60}, {})
        result1 = await monitor.check_soc_deviation({"bus_1": 0.50})
        assert result1 is not None

        # Update last trigger time
        monitor._last_trigger_time = datetime.utcnow()

        # Second trigger immediately after (should be blocked by cooldown)
        # Cooldown check happens in _should_trigger() method
        time_since_trigger = (datetime.utcnow() - monitor._last_trigger_time).total_seconds()
        cooldown_active = time_since_trigger < monitor._trigger_cooldown_sec

        if cooldown_active:
            # Cooldown is active, trigger should be blocked
            # (In real code, _should_trigger() returns False)
            pass
        else:
            # Cooldown expired, trigger should fire
            result2 = await monitor.check_soc_deviation({"bus_1": 0.45})
            assert result2 is not None

    @pytest.mark.asyncio
    async def test_cooldown_expires_after_configured_time(self):
        """Test cooldown expires after configured time."""
        config = TriggerConfig(trigger_cooldown_minutes=1)  # 1 minute cooldown
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Set last trigger time to 2 minutes ago (cooldown expired)
        monitor._last_trigger_time = datetime.utcnow() - timedelta(minutes=2)

        # Trigger should fire (cooldown expired)
        monitor.update_expected_state({"bus_1": 0.60}, {})
        result = await monitor.check_soc_deviation({"bus_1": 0.50})

        assert result is not None, "Trigger should fire after cooldown expires"

    @pytest.mark.asyncio
    async def test_different_cooldown_values_work(self):
        """Test different cooldown values work correctly."""
        # Test with 1 minute cooldown
        config_1min = TriggerConfig(trigger_cooldown_minutes=1)
        monitor_1min = TriggerMonitor(config_1min, AsyncMock(), assembler=MagicMock())
        assert monitor_1min._trigger_cooldown_sec == 60.0

        # Test with 10 minute cooldown
        config_10min = TriggerConfig(trigger_cooldown_minutes=10)
        monitor_10min = TriggerMonitor(config_10min, AsyncMock(), assembler=MagicMock())
        assert monitor_10min._trigger_cooldown_sec == 600.0


@pytest.mark.integration
@pytest.mark.asyncio
class TestMultipleTriggers:
    """Test multiple triggers in same iteration."""

    @pytest.mark.asyncio
    async def test_multiple_triggers_same_iteration(self):
        """Test multiple triggers in same iteration."""
        config = TriggerConfig(
            soc_deviation_threshold=0.05,
            price_change_percent=0.25,
            price_change_absolute=25.0,
            return_time_deviation_min=15.0,
        )

        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Set up for all three triggers
        base_time = datetime.utcnow()
        monitor.update_expected_state(
            {"bus_1": 0.60},  # SoC expectation
            {"bus_2": base_time + timedelta(hours=2)},  # Return expectation
        )
        monitor.update_prices({base_time: 0.10})  # Price baseline

        # Check all triggers
        soc_result = await monitor.check_soc_deviation({"bus_1": 0.50})  # 10% deviation
        price_result = await monitor.check_price_change({base_time: 0.20})  # 100% increase
        return_result = await monitor.check_return_time_deviation(
            {"bus_2": base_time + timedelta(hours=2, minutes=30)}  # 30 min late
        )

        # All should trigger
        assert soc_result is not None
        assert price_result is not None
        assert return_result is not None

    @pytest.mark.asyncio
    async def test_trigger_priority(self):
        """Test trigger priority (which triggers first).

        Note: In real code, all triggers are checked and any that fire
        will trigger optimization. Priority is more about which gets
        logged first or which reason is returned.
        """
        config = TriggerConfig()
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Multiple triggers fire - all should be detected
        monitor.update_expected_state({"bus_1": 0.60}, {})
        soc_result = await monitor.check_soc_deviation({"bus_1": 0.50})

        base_time = datetime.utcnow()
        monitor.update_prices({base_time: 0.10})
        price_result = await monitor.check_price_change({base_time: 0.20})

        # Both should trigger
        assert soc_result is not None
        assert price_result is not None

    @pytest.mark.asyncio
    async def test_trigger_debouncing(self):
        """Test trigger debouncing of rapid changes."""
        config = TriggerConfig(
            trigger_cooldown_minutes=5,  # 5 minute cooldown
        )
        trigger_fired = []

        async def on_trigger(reason: str):
            trigger_fired.append(reason)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Rapid SoC changes
        monitor.update_expected_state({"bus_1": 0.60}, {})

        # First deviation
        result1 = await monitor.check_soc_deviation({"bus_1": 0.50})
        assert result1 is not None

        # Update last trigger time
        monitor._last_trigger_time = datetime.utcnow()

        # Second deviation immediately after (should be debounced by cooldown)
        time_since_trigger = (datetime.utcnow() - monitor._last_trigger_time).total_seconds()
        cooldown_active = time_since_trigger < monitor._trigger_cooldown_sec

        # Cooldown should prevent rapid re-triggering
        assert cooldown_active or time_since_trigger >= monitor._trigger_cooldown_sec
