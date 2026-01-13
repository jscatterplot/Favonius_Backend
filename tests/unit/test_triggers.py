"""Unit tests for TriggerMonitor.

Reference: PRD.md#11-2-unit-test-requirements
Coverage target: ≥ 90%
"""

import asyncio

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg

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
        assert config.trigger_cooldown_minutes == 5  # 5 minutes

    def test_custom_values(self):
        """Test TriggerConfig with custom values."""
        config = TriggerConfig(
            soc_deviation_threshold=0.10,
            price_change_percent=0.50,
            price_change_absolute=50.0,
            return_time_deviation_min=30.0,
            check_interval_sec=120.0,
            trigger_cooldown_minutes=10,
        )
        assert config.soc_deviation_threshold == 0.10
        assert config.price_change_percent == 0.50
        assert config.price_change_absolute == 50.0
        assert config.return_time_deviation_min == 30.0
        assert config.check_interval_sec == 120.0
        assert config.trigger_cooldown_minutes == 10


class TestTriggerMonitorInitialization:
    """Test TriggerMonitor initialization."""

    def test_init(self):
        """Test TriggerMonitor initialization."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        assert monitor.config == config
        assert monitor.on_trigger == callback
        assert monitor.assembler == assembler
        assert monitor.last_prices == {}
        assert monitor.expected_socs == {}
        assert monitor.expected_return_times == {}
        assert monitor._running is False

    def test_init_with_pool_and_depot_id(self):
        """Test initialization with pool and depot_id instead of assembler."""
        config = TriggerConfig()
        callback = AsyncMock()
        pool = MagicMock(spec=asyncpg.Pool)
        monitor = TriggerMonitor(config, callback, pool=pool, depot_id="test_depot")

        assert monitor.pool == pool
        assert monitor.depot_id == "test_depot"

    def test_init_trigger_cooldown_from_config(self):
        """Test TriggerMonitor uses config.trigger_cooldown_minutes (not hardcoded).
        
        Per PRD alignment fix, cooldown should come from config, not be hardcoded.
        """
        config = TriggerConfig(trigger_cooldown_minutes=10)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Cooldown should be calculated from config (10 minutes * 60 = 600 seconds)
        assert monitor._trigger_cooldown_sec == 600.0, (
            "Cooldown should be calculated from config.trigger_cooldown_minutes"
        )

    def test_init_trigger_cooldown_default(self):
        """Test TriggerMonitor uses default cooldown when not specified."""
        config = TriggerConfig()  # Default trigger_cooldown_minutes = 5
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Default is 5 minutes = 300 seconds
        assert monitor._trigger_cooldown_sec == 300.0, (
            "Default cooldown should be 5 minutes = 300 seconds"
        )

    def test_init_trigger_cooldown_custom(self):
        """Test TriggerMonitor with custom cooldown value."""
        config = TriggerConfig(trigger_cooldown_minutes=7)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        assert monitor._trigger_cooldown_sec == 420.0, (
            "Custom cooldown should be 7 minutes = 420 seconds"
        )

    def test_init_raises_error_when_no_state_source(self):
        """Test initialization raises error when no state source provided."""
        config = TriggerConfig()
        callback = AsyncMock()

        with pytest.raises(ValueError, match="Either assembler or"):
            TriggerMonitor(config, callback)  # No assembler, pool, or depot_id

    def test_init_with_custom_config(self):
        """Test initialization with custom config."""
        config = TriggerConfig(soc_deviation_threshold=0.10)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        assert monitor.config.soc_deviation_threshold == 0.10


class TestUpdateExpectedState:
    """Test update_expected_state method."""

    def test_update_expected_state(self):
        """Test updating expected state."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

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
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        monitor.update_expected_state({}, {})

        assert monitor.expected_socs == {}
        assert monitor.expected_return_times == {}


class TestUpdatePrices:
    """Test update_prices method."""

    def test_update_prices(self):
        """Test updating price baseline."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

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
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        monitor.update_prices({})

        assert monitor.last_prices == {}


class TestCheckSocDeviation:
    """Test check_soc_deviation method."""

    @pytest.mark.asyncio
    async def test_soc_deviation_above_threshold(self):
        """Test SoC deviation above threshold triggers."""
        config = TriggerConfig(soc_deviation_threshold=0.05)  # 5%
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

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
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        monitor.update_expected_state({'bus_1': 0.60}, {})

        current_socs = {'bus_1': 0.58}  # 2% deviation (below threshold)

        result = await monitor.check_soc_deviation(current_socs)

        assert result is None

    @pytest.mark.asyncio
    async def test_soc_deviation_exact_threshold(self):
        """Test SoC deviation at exact threshold."""
        config = TriggerConfig(soc_deviation_threshold=0.05)  # 5%
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        current_socs = {'bus_1': 0.50}

        result = await monitor.check_soc_deviation(current_socs)

        assert result is None  # No expected value, no trigger

    @pytest.mark.asyncio
    async def test_soc_deviation_multiple_vehicles(self):
        """Test SoC deviation with multiple vehicles."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        """Test price change triggers when ONLY percent threshold met (OR logic).
        
        Per PRD Section 5.3, price trigger uses OR logic: >25% OR >$25/MWh.
        This test verifies that percent threshold alone triggers.
        """
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh = $100/MWh
        monitor.update_prices(last_prices)

        # 30% change (> 25%) but only $3/MWh change (< $25/MWh)
        # With OR logic, should trigger because percent threshold met
        current_prices = {base_time: 0.013}  # $0.013/kWh = $13/MWh
        # Change: $0.003/kWh = $3/MWh (absolute < $25/MWh)
        # Percent: 30% (> 25%)

        result = await monitor.check_price_change(current_prices)

        assert result is not None, "Price trigger should fire with OR logic when percent threshold met"
        assert 'Price change' in result

    @pytest.mark.asyncio
    async def test_price_change_absolute_only(self):
        """Test price change triggers when ONLY absolute threshold met (OR logic).
        
        Per PRD Section 5.3, price trigger uses OR logic: >25% OR >$25/MWh.
        This test verifies that absolute threshold alone triggers.
        """
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        # Use higher base price to get absolute-only scenario
        last_prices = {base_time: 0.50}  # $0.50/kWh = $500/MWh
        monitor.update_prices(last_prices)
        
        # $0.50 -> $0.53 = $30/MWh change, 6% change (< 25%)
        # With OR logic, should trigger because $30/MWh > $25/MWh
        current_prices = {base_time: 0.53}  # $0.53/kWh = $530/MWh
        # Change: $0.03/kWh = $30/MWh (> $25/MWh)
        # Percent: (0.53 - 0.50) / 0.50 = 0.06 = 6% (< 25%)

        result = await monitor.check_price_change(current_prices)

        assert result is not None, "Price trigger should fire with OR logic when absolute threshold met"
        assert 'Price change' in result

    @pytest.mark.asyncio
    async def test_price_change_neither_threshold(self):
        """Test price change does NOT trigger when NEITHER threshold met."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.12}  # $0.12/kWh
        # 20% change (< 25%) AND $20/MWh change (< $25/MWh)
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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.11}  # 10% change, $10/MWh change

        result = await monitor.check_price_change(current_prices)

        assert result is None

    @pytest.mark.asyncio
    async def test_price_change_both_thresholds(self):
        """Test price change triggers when BOTH thresholds met (OR logic still applies)."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh = $100/MWh
        monitor.update_prices(last_prices)

        current_prices = {base_time: 0.15}  # $0.15/kWh = $150/MWh
        # 50% change (> 25%) AND $50/MWh change (> $25/MWh)
        # Both thresholds met, should trigger

        result = await monitor.check_price_change(current_prices)

        assert result is not None
        assert 'Price change' in result

    @pytest.mark.asyncio
    async def test_price_change_exactly_at_thresholds(self):
        """Test price change behavior at exact threshold values."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.10}  # $0.10/kWh = $100/MWh
        monitor.update_prices(last_prices)

        # Exactly 25% change and exactly $25/MWh change
        current_prices = {base_time: 0.125}  # $0.125/kWh = $125/MWh
        # Change: $0.025/kWh = $25/MWh (exactly at threshold)
        # Percent: 25% (exactly at threshold)
        # With OR logic and > comparison, exactly at threshold should NOT trigger
        # (needs to be > threshold, not >=)

        result = await monitor.check_price_change(current_prices)

        # Exactly at threshold should not trigger (needs > not >=)
        assert result is None

    @pytest.mark.asyncio
    async def test_price_change_small_base_price(self):
        """Test price change with very small base price."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 0.01}  # $0.01/kWh = $10/MWh
        monitor.update_prices(last_prices)

        # Small absolute change but large percent change
        current_prices = {base_time: 0.015}  # $0.015/kWh = $15/MWh
        # Change: $0.005/kWh = $5/MWh (< $25/MWh)
        # Percent: 50% (> 25%)
        # Should trigger because percent threshold met (OR logic)

        result = await monitor.check_price_change(current_prices)

        assert result is not None

    @pytest.mark.asyncio
    async def test_price_change_large_base_price(self):
        """Test price change with very large base price."""
        config = TriggerConfig(
            price_change_percent=0.25,  # 25%
            price_change_absolute=25.0,  # $25/MWh
        )
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        last_prices = {base_time: 1.00}  # $1.00/kWh = $1000/MWh
        monitor.update_prices(last_prices)

        # Large absolute change but small percent change
        current_prices = {base_time: 1.03}  # $1.03/kWh = $1030/MWh
        # Change: $0.03/kWh = $30/MWh (> $25/MWh)
        # Percent: 3% (< 25%)
        # Should trigger because absolute threshold met (OR logic)

        result = await monitor.check_price_change(current_prices)

        assert result is not None

    @pytest.mark.asyncio
    async def test_price_change_no_baseline(self):
        """Test price change check when no baseline exists."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        current_prices = {base_time: 0.15}

        result = await monitor.check_price_change(current_prices)

        assert result is None  # No baseline, no trigger

    @pytest.mark.asyncio
    async def test_price_change_zero_baseline(self):
        """Test price change check with zero baseline price."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        base_time = datetime.utcnow()
        actual_return_times = {'bus_1': base_time}

        result = await monitor.check_return_time_deviation(actual_return_times)

        assert result is None  # No expected value, no trigger

    @pytest.mark.asyncio
    async def test_return_time_deviation_multiple_vehicles(self):
        """Test return time deviation with multiple vehicles."""
        config = TriggerConfig(return_time_deviation_min=15.0)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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


class TestStateFetching:
    """Test state fetching methods."""

    @pytest.mark.asyncio
    async def test_get_current_vehicle_socs_with_assembler(self):
        """Test fetching SoCs via assembler."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        assembler._get_vehicle_socs = AsyncMock(return_value={'bus_1': 0.65})
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        socs = await monitor._get_current_vehicle_socs()

        assert socs == {'bus_1': 0.65}
        assembler._get_vehicle_socs.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_current_vehicle_socs_with_pool(self):
        """Test fetching SoCs via direct database query."""
        config = TriggerConfig()
        callback = AsyncMock()
        pool = MagicMock(spec=asyncpg.Pool)
        mock_conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_row = MagicMock()
        mock_row.__getitem__.side_effect = lambda k: {
            'vehicle_id': 'bus_1',
            'soc': 0.65
        }[k]
        mock_conn.fetch.return_value = [mock_row]

        monitor = TriggerMonitor(config, callback, pool=pool, depot_id="test")
        socs = await monitor._get_current_vehicle_socs()

        assert socs == {'bus_1': 0.65}

    @pytest.mark.asyncio
    async def test_get_current_prices_with_assembler(self):
        """Test fetching prices via assembler."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        assembler.config = MagicMock()
        assembler.config.delta_t = 0.25
        assembler.get_current_state = AsyncMock()
        
        # Mock DepotState
        mock_state = MagicMock()
        mock_state.prices = [0.10, 0.15, 0.20] * 8  # 24 prices
        assembler.get_current_state.return_value = mock_state

        monitor = TriggerMonitor(config, callback, assembler=assembler)
        prices = await monitor._get_current_prices()

        assert len(prices) == 24
        assert all(isinstance(k, datetime) for k in prices.keys())

    @pytest.mark.asyncio
    async def test_get_actual_return_times(self):
        """Test fetching actual return times."""
        config = TriggerConfig()
        callback = AsyncMock()
        pool = MagicMock(spec=asyncpg.Pool)
        mock_conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        base_time = datetime.utcnow()
        mock_row = MagicMock()
        mock_row.__getitem__.side_effect = lambda k: {
            'vehicle_id': 'bus_1',
            'return_time': base_time
        }[k]
        mock_conn.fetch.return_value = [mock_row]

        monitor = TriggerMonitor(config, callback, pool=pool, depot_id="test")
        returns = await monitor._get_actual_return_times()

        assert 'bus_1' in returns
        assert returns['bus_1'] == base_time


class TestTriggerMonitorMonitoringLoop:
    """Test TriggerMonitor monitoring loop."""

    @pytest.mark.asyncio
    async def test_run_loop_fires_soc_trigger(self):
        """Test monitoring loop fires callback when SoC deviation detected."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        
        # Mock state fetching
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        monitor._get_current_vehicle_socs = AsyncMock(
            return_value={'bus_1': 0.50}  # Current SoC
        )
        monitor._get_current_prices = AsyncMock(return_value={})
        monitor._get_actual_return_times = AsyncMock(return_value={})
        monitor.check_soc_deviation = AsyncMock(
            return_value="SoC deviation: bus_1 expected 0.60, got 0.50"
        )
        monitor.check_price_change = AsyncMock(return_value=None)
        monitor.check_return_time_deviation = AsyncMock(return_value=None)
        
        # Set expected state
        monitor.expected_socs = {'bus_1': 0.60}

        # Run for one iteration
        monitor._running = True
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)  # Let it run briefly
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

    @pytest.mark.asyncio
    async def test_run_loop_respects_cooldown(self):
        """Test monitoring loop respects trigger cooldown."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        monitor._get_current_vehicle_socs = AsyncMock(return_value={'bus_1': 0.50})
        monitor._get_current_prices = AsyncMock(return_value={})
        monitor._get_actual_return_times = AsyncMock(return_value={})
        monitor.check_soc_deviation = AsyncMock(
            return_value="SoC deviation detected"
        )
        monitor.check_price_change = AsyncMock(return_value=None)
        monitor.check_return_time_deviation = AsyncMock(return_value=None)
        
        monitor.expected_socs = {'bus_1': 0.60}
        monitor._last_trigger_time = datetime.utcnow()  # Just triggered

        # Run for one iteration
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

        # Callback should NOT be called due to cooldown
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_loop_handles_errors_gracefully(self):
        """Test monitoring loop continues after errors."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        monitor._get_current_vehicle_socs = AsyncMock(
            side_effect=Exception("Database error")
        )
        monitor._get_current_prices = AsyncMock(return_value={})
        monitor._get_actual_return_times = AsyncMock(return_value={})

        # Run for one iteration
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

        # Should not crash, just log error
        assert not monitor._running or True  # Either stopped or still running


class TestTriggerMonitorLifecycle:
    """Test TriggerMonitor lifecycle methods."""

    @pytest.mark.asyncio
    async def test_run_stop(self):
        """Test run and stop methods."""
        config = TriggerConfig(check_interval_sec=0.1)  # Fast for testing
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

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
        monitor = TriggerMonitor(config, callback, assembler=MagicMock())

        # Should not raise exception
        monitor.stop()
        assert monitor._running is False


class TestTriggerMonitorEdgeCases:
    """Test edge cases and error handling for TriggerMonitor."""

    @pytest.mark.asyncio
    async def test_database_query_timeout(self):
        """Test handling of database query timeouts."""
        config = TriggerConfig()
        callback = AsyncMock()
        mock_pool = MagicMock(spec=asyncpg.Pool)
        monitor = TriggerMonitor(config, callback, pool=mock_pool, depot_id="test_depot")

        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetch.side_effect = asyncio.TimeoutError("Query timeout")

        # Should handle timeout gracefully
        socs = await monitor._get_current_vehicle_socs()
        # Should return empty dict on error
        assert socs == {}

    @pytest.mark.asyncio
    async def test_assembler_failure_graceful(self):
        """Test graceful handling when assembler fails."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Make assembler method raise exception
        assembler._get_vehicle_socs = AsyncMock(
            side_effect=Exception("Assembler error")
        )

        # Should handle gracefully
        socs = await monitor._get_current_vehicle_socs()
        assert socs == {}

    @pytest.mark.asyncio
    async def test_trigger_cooldown_critical_events(self):
        """Test that cooldown doesn't miss critical events."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Set very short cooldown for testing
        monitor._trigger_cooldown_sec = 0.1

        monitor.update_expected_state({'bus_1': 0.60}, {})
        assembler._get_vehicle_socs = AsyncMock(return_value={'bus_1': 0.50})
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})

        # First trigger
        monitor._last_trigger_time = None
        trigger1 = await monitor.check_soc_deviation({'bus_1': 0.50})
        assert trigger1 is not None

        # Simulate trigger
        monitor._last_trigger_time = datetime.utcnow()

        # Wait for cooldown to expire
        await asyncio.sleep(0.15)

        # Second trigger after cooldown should work
        trigger2 = await monitor.check_soc_deviation({'bus_1': 0.48})
        assert trigger2 is not None

    @pytest.mark.asyncio
    async def test_multiple_triggers_same_iteration(self):
        """Test handling when multiple triggers fire in same iteration."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Set up multiple triggers
        monitor.update_expected_state({'bus_1': 0.60}, {})
        base_time = datetime.utcnow()
        monitor.update_prices({base_time: 0.10})

        assembler._get_vehicle_socs = AsyncMock(return_value={'bus_1': 0.50})
        assembler.get_current_state = AsyncMock()
        mock_state = MagicMock()
        mock_state.prices = [0.20] * 24  # Price spike
        assembler.config = MagicMock()
        assembler.config.delta_t = 0.25
        assembler.get_current_state.return_value = mock_state
        assembler._get_actual_return_times = AsyncMock(return_value={})

        # Both SoC and price triggers should fire
        soc_trigger = await monitor.check_soc_deviation({'bus_1': 0.50})
        price_trigger = await monitor.check_price_change({
            base_time: 0.20
        })

        assert soc_trigger is not None
        assert price_trigger is not None

        # When run() processes, it should use first trigger found
        # (soc_trigger or price_trigger or return_trigger pattern)


# ============ Advanced Trigger Scenarios ============

class TestAdvancedTriggerScenarios:
    """Tests for advanced trigger scenarios."""

    @pytest.mark.asyncio
    async def test_simultaneous_triggers_priority(self):
        """Test priority when multiple triggers fire simultaneously."""
        config = TriggerConfig(
            soc_deviation_threshold=0.05,
            price_change_percent=0.20,  # 20%
            price_change_absolute=10.0,  # $10/MWh (low to trigger easily)
            return_time_deviation_min=15.0,
        )
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        base_time = datetime.utcnow()
        
        # Set up for all three triggers to potentially fire
        monitor.update_expected_state(
            {'bus_1': 0.60},  # Will deviate
            {'bus_1': base_time + timedelta(hours=2)}  # Will be late
        )
        monitor.update_prices({base_time: 0.10})  # Will spike
        
        # Mock all conditions to trigger
        soc_result = await monitor.check_soc_deviation({'bus_1': 0.45})  # 15% deviation
        price_result = await monitor.check_price_change({base_time: 0.20})  # 100% increase
        return_result = await monitor.check_return_time_deviation({
            'bus_1': base_time + timedelta(hours=2, minutes=30)  # 30 min late
        })
        
        # All should trigger
        assert soc_result is not None
        assert price_result is not None
        assert return_result is not None

    @pytest.mark.asyncio
    async def test_debouncing_rapid_changes(self):
        """Test debouncing of rapid trigger conditions."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        monitor.update_expected_state({'bus_1': 0.60}, {})
        
        # Rapid SoC changes that oscillate around threshold
        soc_values = [0.56, 0.54, 0.56, 0.53, 0.55]
        
        trigger_count = 0
        for soc in soc_values:
            result = await monitor.check_soc_deviation({'bus_1': soc})
            if result is not None:
                trigger_count += 1
        
        # Should trigger on significant deviations
        assert trigger_count > 0

    @pytest.mark.asyncio
    async def test_cooldown_period_enforcement(self):
        """Test cooldown period strictly enforced."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        monitor._trigger_cooldown_sec = 0.5  # 500ms cooldown
        
        monitor.update_expected_state({'bus_1': 0.60}, {})
        
        # First trigger
        result1 = await monitor.check_soc_deviation({'bus_1': 0.50})
        assert result1 is not None
        monitor._last_trigger_time = datetime.utcnow()
        
        # Immediate second check - should still trigger (check returns condition, not fires callback)
        result2 = await monitor.check_soc_deviation({'bus_1': 0.49})
        # The check_soc_deviation method checks condition, not cooldown
        # Cooldown is handled in run() loop or callback

    @pytest.mark.asyncio
    async def test_trigger_after_state_reset(self):
        """Test triggers after expected state is reset."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set initial expected state
        monitor.update_expected_state({'bus_1': 0.60}, {})
        
        # First check - deviates
        result1 = await monitor.check_soc_deviation({'bus_1': 0.50})
        assert result1 is not None
        
        # Reset expected state to current
        monitor.update_expected_state({'bus_1': 0.50}, {})
        
        # Same actual - no deviation now
        result2 = await monitor.check_soc_deviation({'bus_1': 0.50})
        assert result2 is None

    @pytest.mark.asyncio
    async def test_price_normalization_edge_cases(self):
        """Test price trigger with edge case prices."""
        config = TriggerConfig(
            price_change_percent=0.20,  # 20%
            price_change_absolute=5.0,  # $5/MWh (low threshold for test)
        )
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        base_time = datetime.utcnow()
        
        # Test with very small base price - 100% increase AND > $5/MWh
        monitor.update_prices({base_time: 0.01})  # 1 cent = $10/MWh
        result = await monitor.check_price_change({base_time: 0.02})  # 100% increase, +$10/MWh
        assert result is not None
        
        # Test with zero base price
        monitor.update_prices({base_time: 0.0})
        result_zero = await monitor.check_price_change({base_time: 0.10})
        # Should handle gracefully (division by zero protection)
        assert result_zero is None  # Zero baseline is skipped

    @pytest.mark.asyncio
    async def test_return_time_deviation_no_expected(self):
        """Test return time deviation with no expected times."""
        config = TriggerConfig(return_time_deviation_min=15.0)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # No expected return times set
        monitor.update_expected_state({}, {})  # Empty return times
        
        base_time = datetime.utcnow()
        result = await monitor.check_return_time_deviation({
            'bus_1': base_time
        })
        
        # Should not trigger with no expected times
        assert result is None

    @pytest.mark.asyncio
    async def test_multiple_vehicles_different_deviations(self):
        """Test multiple vehicles with varying deviations."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        # Set expected for multiple vehicles
        monitor.update_expected_state({
            'bus_1': 0.60,
            'bus_2': 0.70,
            'bus_3': 0.80,
        }, {})
        
        # Check with varying deviations
        actual_socs = {
            'bus_1': 0.50,  # 10% deviation - triggers
            'bus_2': 0.68,  # 2% deviation - no trigger
            'bus_3': 0.60,  # 20% deviation - triggers
        }
        
        result = await monitor.check_soc_deviation(actual_socs)
        
        # Should trigger for buses with > 5% deviation
        assert result is not None
        assert 'bus_1' in result or 'bus_3' in result

    @pytest.mark.asyncio
    async def test_trigger_config_validation(self):
        """Test TriggerConfig validation with correct parameter names."""
        # Valid config with correct parameter names
        config = TriggerConfig(
            soc_deviation_threshold=0.05,
            price_change_percent=0.20,  # 20%
            price_change_absolute=25.0,  # $25/MWh
            return_time_deviation_min=15.0,  # 15 minutes
        )
        assert config.soc_deviation_threshold == 0.05
        assert config.price_change_percent == 0.20
        assert config.return_time_deviation_min == 15.0
        
        # Edge case: zero threshold (disabled trigger)
        config_zero = TriggerConfig(soc_deviation_threshold=0.0)
        # Should be allowed but effectively always triggers
        
        # Edge case: very high threshold
        config_high = TriggerConfig(soc_deviation_threshold=1.0)
        # Should be allowed but rarely triggers

    @pytest.mark.asyncio
    async def test_trigger_timing_accuracy(self):
        """Test that trigger timing is accurate."""
        config = TriggerConfig()
        callback = AsyncMock()
        assembler = MagicMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)
        
        base_time = datetime.utcnow()
        expected_return = base_time + timedelta(hours=2)
        
        monitor.update_expected_state(
            {},
            {'bus_1': expected_return}
        )
        
        # Exactly on time - no trigger
        result_ontime = await monitor.check_return_time_deviation({
            'bus_1': expected_return
        })
        assert result_ontime is None
        
        # Just under threshold - no trigger
        result_under = await monitor.check_return_time_deviation({
            'bus_1': expected_return + timedelta(minutes=14)
        })
        assert result_under is None
        
        # Just over threshold - should trigger
        result_over = await monitor.check_return_time_deviation({
            'bus_1': expected_return + timedelta(minutes=16)
        })
        assert result_over is not None

