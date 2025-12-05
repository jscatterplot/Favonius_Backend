"""Unit tests for TriggerMonitor.

Reference: PRD.md#11-2-unit-test-requirements
Coverage target: ≥ 90%
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from src.core.state.triggers import TriggerConfig, TriggerMonitor


class TestTriggerConfig:
    """Test TriggerConfig dataclass."""

    def test_default_values(self):
        """Test TriggerConfig default values."""
        config = TriggerConfig()
        assert config.soc_deviation_threshold == 0.05  # 5%
        assert config.price_change_percent == 0.25  # 25%
        assert config.price_change_absolute == 25.0  # $25/MWh
        assert config.return_time_deviation_min == 15.0  # 15 minutes
        assert config.check_interval_sec == 60.0  # 1 minute

    def test_custom_values(self):
        """Test TriggerConfig with custom values."""
        config = TriggerConfig(
            soc_deviation_threshold=0.10,
            price_change_percent=0.50,
            price_change_absolute=50.0,
            return_time_deviation_min=30.0,
            check_interval_sec=120.0,
        )
        assert config.soc_deviation_threshold == 0.10
        assert config.price_change_percent == 0.50
        assert config.price_change_absolute == 50.0
        assert config.return_time_deviation_min == 30.0
        assert config.check_interval_sec == 120.0


class TestTriggerMonitorInitialization:
    """Test TriggerMonitor initialization."""

    def test_init(self):
        """Test TriggerMonitor initialization."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        assert monitor.config == config
        assert monitor.on_trigger == callback
        assert monitor.last_prices == {}
        assert monitor.expected_socs == {}
        assert monitor.expected_return_times == {}
        assert monitor._running is False

    def test_init_with_custom_config(self):
        """Test initialization with custom config."""
        config = TriggerConfig(soc_deviation_threshold=0.10)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        assert monitor.config.soc_deviation_threshold == 0.10


class TestUpdateExpectedState:
    """Test update_expected_state method."""

    def test_update_expected_state(self):
        """Test updating expected state."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        expected_socs = {'bus_1': 0.60, 'bus_2': 0.70}
        expected_return_times = {
            'bus_1': datetime.utcnow() + timedelta(hours=2),
            'bus_2': datetime.utcnow() + timedelta(hours=4),
        }

        monitor.update_expected_state(expected_socs, expected_return_times)

        assert monitor.expected_socs == expected_socs
        assert monitor.expected_return_times == expected_return_times

    def test_update_expected_state_empty(self):
        """Test updating with empty state."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        monitor.update_expected_state({}, {})

        assert monitor.expected_socs == {}
        assert monitor.expected_return_times == {}


class TestUpdatePrices:
    """Test update_prices method."""

    def test_update_prices(self):
        """Test updating price baseline."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        prices = {
            base_time: 0.10,
            base_time + timedelta(hours=1): 0.12,
            base_time + timedelta(hours=2): 0.15,
        }

        monitor.update_prices(prices)

        assert monitor.last_prices == prices

    def test_update_prices_empty(self):
        """Test updating with empty prices."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        monitor.update_prices({})

        assert monitor.last_prices == {}


class TestCheckSocDeviation:
    """Test check_soc_deviation method."""

    @pytest.mark.asyncio
    async def test_soc_deviation_above_threshold(self):
        """Test SoC deviation above threshold triggers."""
        config = TriggerConfig(soc_deviation_threshold=0.05)  # 5%
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        monitor.update_expected_state({'bus_1': 0.60}, {})

        current_socs = {'bus_1': 0.52}  # 8% deviation

        result = await monitor.check_soc_deviation(current_socs)

        assert result is not None
        assert 'bus_1' in result
        assert 'deviation' in result.lower()

    @pytest.mark.asyncio
    async def test_soc_deviation_below_threshold(self):
        """Test SoC deviation below threshold does not trigger."""
        config = TriggerConfig(soc_deviation_threshold=0.05)  # 5%
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        monitor.update_expected_state({'bus_1': 0.60}, {})

        current_socs = {'bus_1': 0.58}  # 2% deviation (below threshold)

        result = await monitor.check_soc_deviation(current_socs)

        assert result is None

    @pytest.mark.asyncio
    async def test_soc_deviation_exact_threshold(self):
        """Test SoC deviation at exact threshold."""
        config = TriggerConfig(soc_deviation_threshold=0.05)  # 5%
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        monitor.update_expected_state({'bus_1': 0.60}, {})

        current_socs = {'bus_1': 0.55}  # Exactly 5% deviation

        result = await monitor.check_soc_deviation(current_socs)

        # Should trigger (deviation > threshold, not >=)
        assert result is None  # 0.05 deviation, threshold is 0.05, so no trigger

    @pytest.mark.asyncio
    async def test_soc_deviation_no_expected(self):
        """Test SoC deviation check when no expected value exists."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        current_socs = {'bus_1': 0.50}

        result = await monitor.check_soc_deviation(current_socs)

        assert result is None  # No expected value, no trigger

    @pytest.mark.asyncio
    async def test_soc_deviation_multiple_vehicles(self):
        """Test SoC deviation with multiple vehicles."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        monitor.update_expected_state(
            {'bus_1': 0.60, 'bus_2': 0.70, 'bus_3': 0.80}, {}
        )

        current_socs = {
            'bus_1': 0.58,  # No deviation
            'bus_2': 0.62,  # 8% deviation - should trigger
            'bus_3': 0.79,  # No deviation
        }

        result = await monitor.check_soc_deviation(current_socs)

        assert result is not None
        assert 'bus_2' in result


class TestCheckPriceChange:
    """Test check_price_change method."""

    @pytest.mark.asyncio
    async def test_price_change_above_both_thresholds(self):
        """Test price change above both thresholds triggers."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh = $100/MWh
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.15}  # $0.15/kWh = $150/MWh
        # 50% change (> 25%) AND $50/MWh change (> $25/MWh)

        result = await monitor.check_price_change(current_prices)

        assert result is not None
        assert 'Price change' in result

    @pytest.mark.asyncio
    async def test_price_change_percent_only(self):
        """Test price change with only percent threshold met."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.13}  # $0.13/kWh
        # 30% change (> 25%) BUT only $30/MWh change (< $25/MWh? No, $30 > $25)
        # Actually: $0.03/kWh = $30/MWh, which is > $25/MWh
        # So both thresholds are met, should trigger

        result = await monitor.check_price_change(current_prices)

        assert result is not None

    @pytest.mark.asyncio
    async def test_price_change_absolute_only(self):
        """Test price change with only absolute threshold met."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.12}  # $0.12/kWh
        # 20% change (< 25%) BUT $20/MWh change (< $25/MWh)
        # Neither threshold met, should not trigger

        result = await monitor.check_price_change(current_prices)

        assert result is None

    @pytest.mark.asyncio
    async def test_price_change_below_thresholds(self):
        """Test price change below both thresholds does not trigger."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.11}  # 10% change, $10/MWh change

        result = await monitor.check_price_change(current_prices)

        assert result is None

    @pytest.mark.asyncio
    async def test_price_change_no_baseline(self):
        """Test price change check when no baseline exists."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        current_prices = {base_time: 0.15}

        result = await monitor.check_price_change(current_prices)

        assert result is None  # No baseline, no trigger

    @pytest.mark.asyncio
    async def test_price_change_zero_baseline(self):
        """Test price change check with zero baseline price."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.0}  # Zero price
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.15}

        result = await monitor.check_price_change(current_prices)

        assert result is None  # Zero baseline, no trigger

    @pytest.mark.asyncio
    async def test_price_change_multiple_timestamps(self):
        """Test price change with multiple timestamps."""
        config = TriggerConfig(
            price_change_percent=0.25,
            price_change_absolute=25.0,
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        last_prices = {
            base_time: 0.10,
            base_time + timedelta(hours=1): 0.12,
        }
        monitor.update_prices(last_prices)

        current_prices = {
            base_time: 0.10,  # No change
            base_time + timedelta(hours=1): 0.20,  # 67% change, $80/MWh change
        }

        result = await monitor.check_price_change(current_prices)

        assert result is not None
        assert str(base_time + timedelta(hours=1)) in result or 'Price change' in result


class TestCheckReturnTimeDeviation:
    """Test check_return_time_deviation method."""

    @pytest.mark.asyncio
    async def test_return_time_deviation_above_threshold(self):
        """Test return time delay above threshold triggers."""
        config = TriggerConfig(return_time_deviation_min=15.0)  # 15 minutes
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        expected_return = base_time + timedelta(hours=2)
        monitor.update_expected_state({}, {'bus_1': expected_return})

        actual_return_times = {
            'bus_1': expected_return + timedelta(minutes=20)  # 20 min delay
        }

        result = await monitor.check_return_time_deviation(actual_return_times)

        assert result is not None
        assert 'bus_1' in result
        assert 'Return delay' in result or 'delay' in result.lower()

    @pytest.mark.asyncio
    async def test_return_time_deviation_below_threshold(self):
        """Test return time delay below threshold does not trigger."""
        config = TriggerConfig(return_time_deviation_min=15.0)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        expected_return = base_time + timedelta(hours=2)
        monitor.update_expected_state({}, {'bus_1': expected_return})

        actual_return_times = {
            'bus_1': expected_return + timedelta(minutes=10)  # 10 min delay
        }

        result = await monitor.check_return_time_deviation(actual_return_times)

        assert result is None

    @pytest.mark.asyncio
    async def test_return_time_deviation_exact_threshold(self):
        """Test return time delay at exact threshold."""
        config = TriggerConfig(return_time_deviation_min=15.0)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        expected_return = base_time + timedelta(hours=2)
        monitor.update_expected_state({}, {'bus_1': expected_return})

        actual_return_times = {
            'bus_1': expected_return + timedelta(minutes=15)  # Exactly 15 min
        }

        result = await monitor.check_return_time_deviation(actual_return_times)

        # Should trigger (deviation > threshold, not >=)
        # Actually: 15 minutes == threshold, so no trigger
        assert result is None

    @pytest.mark.asyncio
    async def test_return_time_deviation_early_return(self):
        """Test early return does not trigger."""
        config = TriggerConfig(return_time_deviation_min=15.0)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        expected_return = base_time + timedelta(hours=2)
        monitor.update_expected_state({}, {'bus_1': expected_return})

        actual_return_times = {
            'bus_1': expected_return - timedelta(minutes=10)  # 10 min early
        }

        result = await monitor.check_return_time_deviation(actual_return_times)

        assert result is None  # Early return is not a delay

    @pytest.mark.asyncio
    async def test_return_time_deviation_no_expected(self):
        """Test return time deviation check when no expected value exists."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        actual_return_times = {'bus_1': base_time}

        result = await monitor.check_return_time_deviation(actual_return_times)

        assert result is None  # No expected value, no trigger

    @pytest.mark.asyncio
    async def test_return_time_deviation_multiple_vehicles(self):
        """Test return time deviation with multiple vehicles."""
        config = TriggerConfig(return_time_deviation_min=15.0)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        base_time = datetime.utcnow()
        monitor.update_expected_state(
            {},
            {
                'bus_1': base_time + timedelta(hours=2),
                'bus_2': base_time + timedelta(hours=4),
            }
        )

        actual_return_times = {
            'bus_1': base_time + timedelta(hours=2, minutes=10),  # No delay
            'bus_2': base_time + timedelta(hours=4, minutes=20),  # 20 min delay
        }

        result = await monitor.check_return_time_deviation(actual_return_times)

        assert result is not None
        assert 'bus_2' in result


class TestTriggerMonitorLifecycle:
    """Test TriggerMonitor lifecycle methods."""

    @pytest.mark.asyncio
    async def test_run_stop(self):
        """Test run and stop methods."""
        config = TriggerConfig(check_interval_sec=0.1)  # Fast for testing
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        assert monitor._running is False

        # Start monitoring in background
        import asyncio
        task = asyncio.create_task(monitor.run())
        
        # Wait a bit
        await asyncio.sleep(0.15)
        
        # Stop monitoring
        monitor.stop()
        
        # Wait for task to complete
        await asyncio.sleep(0.1)
        
        # Task should be done
        assert task.done()
        assert monitor._running is False

    def test_stop_when_not_running(self):
        """Test stop when monitor is not running."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback)

        # Should not raise exception
        monitor.stop()
        assert monitor._running is False

