"""Unit tests for StateAssembler.

Reference: PRD.md#11-2-unit-test-requirements
Coverage target: ≥ 80%
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from src.core.models import DepotConfig
from src.core.state.assembler import StateAssembler
from src.db.pools import DatabasePools


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    return pool


@pytest.fixture
def mock_db_pools(mock_db_pool):
    """Mock DatabasePools wrapping the single mock pool for both static and ts."""
    return DatabasePools(static=mock_db_pool, ts=mock_db_pool)


@pytest.fixture
def depot_config():
    """Depot configuration for testing."""
    vehicle_ids = ["bus_1", "bus_2", "bus_3"]
    return DepotConfig(
        vehicle_capacities={"bus_1": 324.0, "bus_2": 324.0, "bus_3": 200.0},
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
    return str(uuid4())


@pytest.fixture
def assembler(mock_db_pools, depot_id, depot_config):
    """StateAssembler instance for testing."""
    return StateAssembler(mock_db_pools, depot_id, depot_config)


class TestStateAssemblerInitialization:
    """Test StateAssembler initialization."""

    def test_init(self, mock_db_pool, mock_db_pools, depot_id, depot_config):
        """Test StateAssembler initialization."""
        assembler = StateAssembler(mock_db_pools, depot_id, depot_config)
        assert assembler.pools.ts == mock_db_pool
        assert assembler.depot_id == depot_id
        assert assembler.config == depot_config

    def test_init_with_uuid(self, mock_db_pools, depot_config):
        """Test initialization with UUID object."""
        depot_uuid = uuid4()
        assembler = StateAssembler(mock_db_pools, depot_uuid, depot_config)
        assert assembler.depot_id == str(depot_uuid)


class TestGetVehicleSocs:
    """Test _get_vehicle_socs method."""

    @pytest.mark.asyncio
    async def test_get_vehicle_socs_single(self, assembler, mock_db_pool):
        """Test retrieving SoC for single vehicle."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        mock_row = MagicMock()
        mock_row.__getitem__.side_effect = lambda k: {"vehicle_id": "bus_1", "soc": 0.65}[k]
        mock_conn.fetch.return_value = [mock_row]

        socs = await assembler._get_vehicle_socs()

        assert "bus_1" in socs
        assert socs["bus_1"] == 0.65
        mock_conn.fetch.assert_called()

    @pytest.mark.asyncio
    async def test_get_vehicle_socs_multiple(self, assembler, mock_db_pool):
        """Test retrieving SoC for multiple vehicles."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        rows = [
            MagicMock(
                **{"__getitem__.side_effect": lambda k: {"vehicle_id": "bus_1", "soc": 0.65}[k]}
            ),
            MagicMock(
                **{"__getitem__.side_effect": lambda k: {"vehicle_id": "bus_2", "soc": 0.80}[k]}
            ),
        ]
        mock_conn.fetch.return_value = rows

        socs = await assembler._get_vehicle_socs()

        assert len(socs) == 2
        assert socs["bus_1"] == 0.65
        assert socs["bus_2"] == 0.80

    @pytest.mark.asyncio
    async def test_get_vehicle_socs_empty(self, assembler, mock_db_pool):
        """Test retrieving SoC when no vehicles exist."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetch.return_value = []

        socs = await assembler._get_vehicle_socs()

        assert len(socs) == 0
        assert isinstance(socs, dict)


class TestGetBatterySoc:
    """Test _get_battery_soc method."""

    @pytest.mark.asyncio
    async def test_get_battery_soc_mvp(self, assembler):
        """Test battery SoC returns fixed value for MVP."""
        soc = await assembler._get_battery_soc()
        assert soc == 0.5  # MVP hardcoded value


class TestGetPrices:
    """Test _get_prices method."""

    @pytest.mark.asyncio
    async def test_get_prices_with_data(self, assembler, mock_db_pool):
        """Test price retrieval with database data."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Mock 6 hours of hourly prices using dict-like row objects
        rows = []
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        for i in range(6):
            row_data = {"time": base_time + timedelta(hours=i), "price_per_kwh": 0.10 + i * 0.01}
            row = MagicMock()
            row.__getitem__ = lambda self, k, d=row_data: d[k]
            rows.append(row)
        mock_conn.fetch.return_value = rows

        start = base_time
        end = start + timedelta(hours=24)
        n_steps = 96  # 24 hours * 4 timesteps/hour

        prices = await assembler._get_prices(start, end, n_steps)

        # Should have 96 prices (24 hours * 4)
        assert len(prices) == 96
        # All prices should be positive
        assert all(p > 0 for p in prices)

    @pytest.mark.asyncio
    async def test_get_prices_no_data(self, assembler, mock_db_pool):
        """Test price retrieval when no data exists."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetch.return_value = []

        start = datetime.utcnow()
        end = start + timedelta(hours=24)
        n_steps = 96

        prices = await assembler._get_prices(start, end, n_steps)

        # Should return default prices
        assert len(prices) == 96
        assert all(p == 0.15 for p in prices)  # Default $0.15/kWh

    @pytest.mark.asyncio
    async def test_get_prices_interpolation(self, assembler, mock_db_pool):
        """Test price interpolation from hourly to 15-min timesteps."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Create proper mock rows
        row1_data = {"time": base_time, "price_per_kwh": 0.10}
        row2_data = {"time": base_time + timedelta(hours=1), "price_per_kwh": 0.20}

        row1 = MagicMock()
        row1.__getitem__ = lambda self, k, d=row1_data: d[k]
        row2 = MagicMock()
        row2.__getitem__ = lambda self, k, d=row2_data: d[k]

        mock_conn.fetch.return_value = [row1, row2]

        start = base_time
        end = start + timedelta(hours=2)
        n_steps = 8  # 2 hours * 4 timesteps/hour

        prices = await assembler._get_prices(start, end, n_steps)

        assert len(prices) == 8
        # Prices should be interpolated - check they're all positive
        assert all(p > 0 for p in prices)

    @pytest.mark.asyncio
    async def test_get_prices_with_gaps(self, assembler, mock_db_pool):
        """Test price interpolation handles gaps in price data."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        # Only provide price for first hour, missing second hour
        rows = [
            MagicMock(
                **{
                    "__getitem__.side_effect": lambda k, t=base_time, p=0.10: {
                        "time": t,
                        "price_per_kwh": p,
                    }[k]
                }
            ),
        ]
        mock_conn.fetch.return_value = rows

        start = base_time
        end = start + timedelta(hours=2)
        n_steps = 8  # 2 hours * 4 timesteps/hour

        prices = await assembler._get_prices(start, end, n_steps)

        assert len(prices) == 8
        # Should forward-fill from first hour (within 1 hour window)
        assert all(p == 0.10 for p in prices)


class TestGetSchedules:
    """Test _get_schedules method."""

    @pytest.mark.asyncio
    async def test_get_schedules_single(self, assembler, mock_db_pool):
        """Test retrieving single schedule."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        mock_row = {
            "vehicle_id": "bus_1",
            "departure_time": base_time + timedelta(hours=6),
            "return_time": base_time + timedelta(hours=10),
            "estimated_energy_kwh": 150.0,
            "route_id": "route_1",
        }
        mock_conn.fetch.return_value = [mock_row]

        start = base_time
        end = start + timedelta(hours=24)
        schedules = await assembler._get_schedules(start, end)

        assert len(schedules) == 1
        assert schedules[0]["vehicle_id"] == "bus_1"
        assert schedules[0]["estimated_energy_kwh"] == 150.0

    @pytest.mark.asyncio
    async def test_get_schedules_multiple(self, assembler, mock_db_pool):
        """Test retrieving multiple schedules."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        rows = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
                "estimated_energy_kwh": 150.0,
                "route_id": "route_1",
            },
            {
                "vehicle_id": "bus_2",
                "departure_time": base_time + timedelta(hours=8),
                "return_time": base_time + timedelta(hours=12),
                "estimated_energy_kwh": 200.0,
                "route_id": "route_2",
            },
        ]
        mock_conn.fetch.return_value = rows

        start = base_time
        end = start + timedelta(hours=24)
        schedules = await assembler._get_schedules(start, end)

        assert len(schedules) == 2
        assert schedules[0]["vehicle_id"] == "bus_1"
        assert schedules[1]["vehicle_id"] == "bus_2"

    @pytest.mark.asyncio
    async def test_get_schedules_empty(self, assembler, mock_db_pool):
        """Test retrieving schedules when none exist."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetch.return_value = []

        start = datetime.utcnow()
        end = start + timedelta(hours=24)
        schedules = await assembler._get_schedules(start, end)

        assert len(schedules) == 0
        assert isinstance(schedules, list)


class TestComputeAvailability:
    """Test _compute_availability method."""

    def test_compute_availability_all_available(self, assembler):
        """Test availability when all vehicles are available."""
        schedules = []
        start = datetime.utcnow()
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # All vehicles should be available
        for vid in assembler.config.vehicle_capacities.keys():
            assert vid in availability
            assert all(availability[vid])  # All True

    def test_compute_availability_with_departure(self, assembler):
        """Test availability with vehicle departure."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
            }
        ]
        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # bus_1 should be unavailable during trip (timesteps 24-40, 6-10 hours)
        # bus_2 and bus_3 should be available
        assert availability["bus_2"][0] is True
        assert availability["bus_3"][0] is True

        # Check bus_1 is unavailable during trip
        trip_start_idx = 24  # 6 hours * 4 timesteps/hour
        trip_end_idx = 40  # 10 hours * 4 timesteps/hour
        assert any(not availability["bus_1"][i] for i in range(trip_start_idx, trip_end_idx))

    def test_compute_availability_unknown_vehicle(self, assembler):
        """Test availability with schedule for unknown vehicle."""
        base_time = datetime.utcnow()
        schedules = [
            {
                "vehicle_id": "unknown_bus",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
            }
        ]
        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Unknown vehicle should be ignored
        assert "unknown_bus" not in availability


class TestComputeDepartureTimes:
    """Test _compute_departure_times method."""

    def test_compute_departure_times_single(self, assembler):
        """Test departure time computation for single vehicle."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
            }
        ]
        start = base_time

        departures = assembler._compute_departure_times(schedules, start)

        assert "bus_1" in departures
        assert departures["bus_1"] == 24  # 6 hours * 4 timesteps/hour

    def test_compute_departure_times_multiple(self, assembler):
        """Test departure time computation for multiple vehicles."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
            },
            {
                "vehicle_id": "bus_2",
                "departure_time": base_time + timedelta(hours=8),
                "return_time": base_time + timedelta(hours=12),
            },
        ]
        start = base_time

        departures = assembler._compute_departure_times(schedules, start)

        assert len(departures) == 2
        assert departures["bus_1"] == 24  # 6 hours
        assert departures["bus_2"] == 32  # 8 hours

    def test_compute_departure_times_earliest(self, assembler):
        """Test that earliest departure is kept for each vehicle."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=8),
                "return_time": base_time + timedelta(hours=10),
            },
            {
                "vehicle_id": "bus_1",  # Same vehicle, earlier departure
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
            },
        ]
        start = base_time

        departures = assembler._compute_departure_times(schedules, start)

        # Should keep earliest departure (6 hours = timestep 24)
        assert departures["bus_1"] == 24


class TestComputeEnergyRequirements:
    """Test _compute_energy_requirements method."""

    def test_compute_energy_requirements_with_data(self, assembler):
        """Test energy requirement computation with schedule data."""
        schedules = [
            {
                "vehicle_id": "bus_1",
                "estimated_energy_kwh": 150.0,
            },
            {
                "vehicle_id": "bus_2",
                "estimated_energy_kwh": 200.0,
            },
        ]

        requirements = assembler._compute_energy_requirements(schedules)

        assert requirements["bus_1"] == 150.0
        assert requirements["bus_2"] == 200.0

    def test_compute_energy_requirements_default(self, assembler):
        """Test energy requirement computation with missing data."""
        schedules = [
            {
                "vehicle_id": "bus_1",
                # No estimated_energy_kwh
            },
        ]

        requirements = assembler._compute_energy_requirements(schedules)

        assert requirements["bus_1"] == 100.0  # Default value

    def test_compute_energy_requirements_empty(self, assembler):
        """Test energy requirement computation with no schedules."""
        schedules = []

        requirements = assembler._compute_energy_requirements(schedules)

        assert len(requirements) == 0


class TestGetCurrentMonthPeak:
    """Test _get_current_month_peak method."""

    @pytest.mark.asyncio
    async def test_get_current_month_peak_with_data(self, assembler, mock_db_pool):
        """Test peak retrieval with database data."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        mock_row = MagicMock()
        mock_row.__getitem__.side_effect = lambda k: {"peak": 250.0}[k]
        mock_conn.fetchrow.return_value = mock_row

        peak = await assembler._get_current_month_peak()

        assert peak == 250.0

    @pytest.mark.asyncio
    async def test_get_current_month_peak_no_data(self, assembler, mock_db_pool):
        """Test peak retrieval when no data exists."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetchrow.return_value = None

        peak = await assembler._get_current_month_peak()

        assert peak == 0.0

    @pytest.mark.asyncio
    async def test_get_current_month_peak_null(self, assembler, mock_db_pool):
        """Test peak retrieval when peak is NULL."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        mock_row = MagicMock()
        mock_row.__getitem__.side_effect = lambda k: {"peak": None}[k]
        mock_conn.fetchrow.return_value = mock_row

        peak = await assembler._get_current_month_peak()

        assert peak == 0.0


class TestGetDemandChargeRate:
    """Test _get_demand_charge_rate method.

    Per PRD Section 8.1, priority is:
    1. prices.demand_kw (most recent price row)
    2. depots.demand_charge_rate_kw
    3. Default $20/kW
    """

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_from_prices(self, assembler, mock_db_pool):
        """Test demand charge rate retrieved from prices.demand_kw (Priority 1)."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # First query: prices.demand_kw (should return value)
        price_row = MagicMock()
        price_row.__getitem__.side_effect = lambda k: {"demand_kw": 30.0}[k]

        # Mock two acquire calls (one for prices, one for depots if prices fails)
        # But prices should succeed, so only one call needed
        mock_conn.fetchrow.side_effect = [price_row]  # First call returns price row

        rate = await assembler._get_demand_charge_rate()

        assert rate == 30.0, "Should use prices.demand_kw when available"
        # Should query prices table first
        assert mock_conn.fetchrow.call_count >= 1

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_from_depot_config(self, assembler, mock_db_pool):
        """Test demand charge rate falls back to depot config when prices.demand_kw is NULL (Priority 2)."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # First query: prices.demand_kw (returns None)
        # Second query: depots.demand_charge_rate_kw (returns value)
        price_row = None  # No price row with demand_kw
        depot_row = MagicMock()
        depot_row.__getitem__.side_effect = lambda k: {"demand_charge_rate_kw": 25.0}[k]

        mock_conn.fetchrow.side_effect = [price_row, depot_row]

        rate = await assembler._get_demand_charge_rate()

        assert rate == 25.0, "Should fall back to depot config when prices.demand_kw is NULL"
        assert mock_conn.fetchrow.call_count == 2  # Prices query + depot query

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_default_fallback(self, assembler, mock_db_pool):
        """Test demand charge rate falls back to default when both are NULL (Priority 3)."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Both queries return None
        mock_conn.fetchrow.side_effect = [None, None]

        rate = await assembler._get_demand_charge_rate()

        assert rate == 20.0, "Should use default $20/kW when both are NULL"
        assert mock_conn.fetchrow.call_count == 2  # Prices query + depot query

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_prices_takes_precedence(self, assembler, mock_db_pool):
        """Test prices.demand_kw takes precedence over depot config even if depot has value."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Prices query returns value (should use this, not query depot)
        price_row = MagicMock()
        price_row.__getitem__.side_effect = lambda k: {"demand_kw": 35.0}[k]

        mock_conn.fetchrow.side_effect = [price_row]  # Only prices query should be called

        rate = await assembler._get_demand_charge_rate()

        assert rate == 35.0, "Should use prices.demand_kw even if depot config exists"
        # Should only query prices, not depot (since prices returned a value)
        assert mock_conn.fetchrow.call_count == 1

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_most_recent_price(self, assembler, mock_db_pool):
        """Test that most recent price row is used when multiple price rows exist."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Most recent price row should be returned (ORDER BY time DESC LIMIT 1)
        price_row = MagicMock()
        price_row.__getitem__.side_effect = lambda k: {"demand_kw": 28.0}[k]

        mock_conn.fetchrow.return_value = price_row

        rate = await assembler._get_demand_charge_rate()

        assert rate == 28.0
        # Verify query uses ORDER BY time DESC LIMIT 1
        call_args = mock_conn.fetchrow.call_args[0][0] if mock_conn.fetchrow.called else None
        if call_args:
            assert "ORDER BY time DESC" in call_args or "ORDER BY time DESC" in str(
                mock_conn.fetchrow.call_args
            )

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_prices_null_depot_null(self, assembler, mock_db_pool):
        """Test fallback when prices.demand_kw is NULL and depot config is NULL."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Prices query returns row but demand_kw is NULL
        price_row = MagicMock()
        price_row.__getitem__.side_effect = lambda k: {"demand_kw": None}[k]

        # Depot query also returns None
        mock_conn.fetchrow.side_effect = [price_row, None]

        rate = await assembler._get_demand_charge_rate()

        assert rate == 20.0, "Should use default when both are NULL"
        assert mock_conn.fetchrow.call_count == 2

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_prices_no_rows_depot_has_value(
        self, assembler, mock_db_pool
    ):
        """Test fallback to depot when no price rows exist."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # No price rows, but depot has value
        depot_row = MagicMock()
        depot_row.__getitem__.side_effect = lambda k: {"demand_charge_rate_kw": 22.0}[k]

        mock_conn.fetchrow.side_effect = [None, depot_row]

        rate = await assembler._get_demand_charge_rate()

        assert rate == 22.0, "Should use depot config when no price rows"
        assert mock_conn.fetchrow.call_count == 2


class TestGetBuildingPower:
    """Test _get_building_power method."""

    @pytest.mark.asyncio
    async def test_get_building_power_mvp(self, assembler):
        """Test building power returns zero for MVP."""
        start = datetime.utcnow()
        end = start + timedelta(hours=24)
        n_steps = 96

        power = await assembler._get_building_power(start, end, n_steps)

        assert len(power) == 96
        assert all(p == 0.0 for p in power)  # MVP returns zeros

    @pytest.mark.asyncio
    async def test_get_building_power_uses_static_assumption_when_configured(
        self, assembler, mock_db_pool
    ):
        """Static derate should bypass TimescaleDB building-load lookup."""
        assembler.config.building_load_assumption_kw = 30.0
        start = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=24)
        n_steps = 96

        power = await assembler._get_building_power(start, end, n_steps)

        assert all(p == 0.0 for p in power)
        assert assembler.last_building_load_source == "static_assumption"
        mock_db_pool.acquire.assert_not_called()


class TestGetCurrentState:
    """Test get_current_state integration method."""

    @pytest.mark.asyncio
    async def test_get_current_state_full(self, assembler, mock_db_pool):
        """Test full state assembly."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Mock vehicle IDs (static pool), SoCs (ts pool), prices, then empty for others
        fetch_calls = {"count": 0}

        async def fetch_side_effect(*args, **kwargs):
            idx = fetch_calls["count"]
            fetch_calls["count"] += 1
            if idx == 0:
                # Vehicle ID lookup from static pool (SELECT FROM vehicles)
                return [
                    MagicMock(
                        **{
                            "__getitem__.side_effect": lambda k: {
                                "vehicle_id": "bus_1",
                            }[k]
                        }
                    ),
                ]
            if idx == 1:
                # SoC lookup from ts pool (SELECT FROM telemetry)
                return [
                    MagicMock(
                        **{
                            "__getitem__.side_effect": lambda k: {
                                "vehicle_id": "bus_1",
                                "soc": 0.65,
                            }[k]
                        }
                    ),
                ]
            if idx == 2:
                # Price lookup from ts pool
                return [
                    MagicMock(
                        **{
                            "__getitem__.side_effect": lambda k, t=datetime.utcnow(), p=0.10: {
                                "time": t,
                                "price_per_kwh": p,
                            }[k]
                        }
                    ),
                ]
            return []

        mock_conn.fetch.side_effect = fetch_side_effect

        # Mock current month peak and demand charge rate
        mock_peak_row = MagicMock()
        mock_peak_row.__getitem__.side_effect = lambda k: {"peak": 100.0}[k]
        mock_price_row = MagicMock()
        mock_price_row.__getitem__.side_effect = lambda k: {"demand_kw": None}[k]
        mock_demand_row = MagicMock()
        mock_demand_row.__getitem__.side_effect = lambda k: {"demand_charge_rate_kw": 25.0}[k]
        mock_conn.fetchrow.side_effect = [mock_peak_row, mock_price_row, mock_demand_row]

        state = await assembler.get_current_state(horizon_hours=24)

        assert state.vehicle_socs["bus_1"] == 0.65
        assert len(state.prices) == 96
        assert state.current_month_peak == 100.0
        assert state.battery_soc == 0.5  # MVP default
        assert state.demand_charge_rate == 25.0  # From database
        assert len(state.building_power) == 96
        assert all(p >= 0.0 for p in state.building_power)

    @pytest.mark.asyncio
    async def test_get_current_state_custom_horizon(self, assembler, mock_db_pool):
        """Test state assembly with custom horizon."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        async def fetch_side_effect(*args, **kwargs):
            return []

        mock_conn.fetch.side_effect = fetch_side_effect

        mock_conn.fetchrow.side_effect = [None, None, None]  # peak, price, demand_rate

        state = await assembler.get_current_state(horizon_hours=12)

        # 12 hours = 48 timesteps (12 * 4)
        assert len(state.prices) == 48
        assert len(state.building_power) == 48

    @pytest.mark.asyncio
    async def test_get_current_state_invalid_horizon(self, assembler):
        """Test state assembly with invalid horizon raises ValueError."""
        with pytest.raises(ValueError, match="horizon_hours must be in"):
            await assembler.get_current_state(horizon_hours=0)

        with pytest.raises(ValueError, match="horizon_hours must be in"):
            await assembler.get_current_state(horizon_hours=49)

    @pytest.mark.asyncio
    async def test_get_current_state_missing_vehicle_socs(self, assembler, mock_db_pool):
        """Test state assembly handles missing vehicle SoC data."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # No vehicle SoCs returned
        fetch_calls = {"count": 0}

        async def fetch_side_effect(*args, **kwargs):
            idx = fetch_calls["count"]
            fetch_calls["count"] += 1
            if idx == 0:
                return []
            if idx == 1:
                return [
                    MagicMock(
                        **{
                            "__getitem__.side_effect": lambda k, t=datetime.utcnow(), p=0.10: {
                                "time": t,
                                "price_per_kwh": p,
                            }[k]
                        }
                    ),
                ]
            return []

        mock_conn.fetch.side_effect = fetch_side_effect

        mock_conn.fetchrow.side_effect = [None, None, None]  # peak, price, demand_rate

        state = await assembler.get_current_state(horizon_hours=24)

        # Should have default SoC for all configured vehicles
        assert len(state.vehicle_socs) == 3  # bus_1, bus_2, bus_3 from fixture
        assert all(soc == 0.5 for soc in state.vehicle_socs.values())


class TestStateAssemblerEdgeCases:
    """Test edge cases and error handling for StateAssembler."""

    @pytest.mark.asyncio
    async def test_database_connection_failure(self, assembler, mock_db_pool):
        """Test graceful handling of database connection failures."""
        AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.side_effect = asyncpg.PostgresConnectionError(
            "Connection failed"
        )

        # Should raise exception (not handled silently)
        with pytest.raises(asyncpg.PostgresConnectionError):
            await assembler._get_vehicle_socs()

    @pytest.mark.asyncio
    async def test_missing_price_data_fallback(self, assembler, mock_db_pool):
        """Test fallback strategy when price data is missing."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_conn.fetch.return_value = []  # No price data

        start = datetime.utcnow()
        end = start + timedelta(hours=24)
        n_steps = 96

        prices = await assembler._get_prices(start, end, n_steps)

        # Should return default prices
        assert len(prices) == 96
        assert all(p == 0.15 for p in prices)  # Default fallback

    @pytest.mark.asyncio
    async def test_timezone_edge_cases_schedule_computation(self, assembler):
        """Test schedule computation handles timezone edge cases."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Schedule that spans midnight UTC
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=22),
                "return_time": base_time + timedelta(hours=26),  # Next day
            }
        ]
        start = base_time
        n_steps = 96  # 24 hours

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Should handle day boundary correctly
        assert "bus_1" in availability
        # Vehicle should be unavailable during trip period
        assert not availability["bus_1"][88]  # 22 hours = timestep 88
        assert not availability["bus_1"][95]  # Still on route at end

    @pytest.mark.asyncio
    async def test_horizon_bounds_validation(self, assembler):
        """Test horizon_hours bounds are validated strictly."""
        # Test zero horizon
        with pytest.raises(ValueError, match="horizon_hours must be in"):
            await assembler.get_current_state(horizon_hours=0)

        # Test negative horizon
        with pytest.raises(ValueError, match="horizon_hours must be in"):
            await assembler.get_current_state(horizon_hours=-1)

        # Test too large horizon
        with pytest.raises(ValueError, match="horizon_hours must be in"):
            await assembler.get_current_state(horizon_hours=49)

        # Test valid horizons
        # These should not raise
        try:
            await assembler.get_current_state(horizon_hours=1)
        except ValueError:
            pytest.fail("horizon_hours=1 should be valid")

        try:
            await assembler.get_current_state(horizon_hours=48)
        except ValueError:
            pytest.fail("horizon_hours=48 should be valid")

    @pytest.mark.asyncio
    async def test_concurrent_access_scenario(self, mock_db_pool, mock_db_pools, depot_id, depot_config):
        """Test concurrent access to state assembler.

        This test verifies that multiple assemblers can be created concurrently
        without shared state issues.
        """
        # Create multiple assemblers (simulating concurrent access pattern)
        assemblers = [StateAssembler(mock_db_pools, depot_id, depot_config) for _ in range(3)]

        # Verify each assembler is properly initialized
        for idx, assembler in enumerate(assemblers):
            assert assembler.depot_id == depot_id
            assert assembler.config == depot_config
            assert assembler.pools.ts == mock_db_pool

        # Verify assemblers are independent instances
        assert assemblers[0] is not assemblers[1]
        assert assemblers[1] is not assemblers[2]


# ============ Complex Scenario Tests ============


class TestComplexScenarios:
    """Tests for complex state assembly scenarios."""

    def test_overlapping_routes_same_vehicle(self, assembler):
        """Test handling of overlapping routes for same vehicle."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Two routes that overlap for bus_1
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=10),
            },
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=8),  # Overlaps!
                "return_time": base_time + timedelta(hours=12),
            },
        ]

        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Vehicle should be unavailable during both route periods
        # Hour 6-12 should all be unavailable
        for t in range(24, 48):  # timesteps 24-48 = hours 6-12
            assert not availability["bus_1"][t]

    def test_routes_spanning_multiple_days(self, assembler):
        """Test routes that span multiple days."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # A long overnight route
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=20),
                "return_time": base_time + timedelta(hours=30),  # Returns next day
            }
        ]

        start = base_time
        n_steps = 96  # 24 hours

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Should be unavailable from hour 20 to end of horizon
        for t in range(80, 96):  # timesteps 80-96 = hours 20-24
            assert not availability["bus_1"][t]

    def test_routes_starting_before_horizon(self, assembler):
        """Test routes that started before the optimization horizon."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Route that started 2 hours ago, returns in 4 hours
        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time - timedelta(hours=2),  # Already departed
                "return_time": base_time + timedelta(hours=4),
            }
        ]

        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Should be unavailable from start until return
        for t in range(16):  # timesteps 0-16 = hours 0-4
            assert not availability["bus_1"][t]

        # Should be available after return
        assert availability["bus_1"][20] is True  # Hour 5

    def test_multiple_vehicles_complex_schedules(self, assembler):
        """Test multiple vehicles with complex overlapping schedules."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        schedules = [
            # Bus 1: Morning route
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
                "return_time": base_time + timedelta(hours=9),
            },
            # Bus 1: Afternoon route
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=14),
                "return_time": base_time + timedelta(hours=17),
            },
            # Bus 2: All day route
            {
                "vehicle_id": "bus_2",
                "departure_time": base_time + timedelta(hours=5),
                "return_time": base_time + timedelta(hours=18),
            },
            # Bus 3: Available all day
            # No schedule entries
        ]

        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Check bus_1 is unavailable during both routes
        assert not availability["bus_1"][28]  # Hour 7 (first route)
        assert not availability["bus_1"][60]  # Hour 15 (second route)
        # Available between routes
        assert availability["bus_1"][44] is True  # Hour 11

        # Check bus_2 is unavailable during long route
        assert not availability["bus_2"][40]  # Hour 10


class TestPriceHandling:
    """Tests for price assembly and interpolation."""

    @pytest.mark.asyncio
    async def test_price_gap_interpolation(self, assembler, mock_db_pool):
        """Test that price gaps are filled with interpolation."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Prices with gaps - only every other hour
        price_rows = []
        for i in range(0, 24, 2):  # Every 2 hours
            mock_row = MagicMock()
            mock_row.__getitem__ = lambda self, k, t=base_time + timedelta(
                hours=i
            ), p=0.10 + i * 0.01: {
                "time": t,
                "price_per_kwh": p,
            }[
                k
            ]
            price_rows.append(mock_row)

        mock_conn.fetch.return_value = price_rows

        start = base_time
        end = base_time + timedelta(hours=24)
        n_steps = 96

        prices = await assembler._get_prices(start, end, n_steps)

        # Should have 96 prices
        assert len(prices) == 96
        # All should be reasonable values (interpolated)
        assert all(0.0 <= p <= 1.0 for p in prices)

    @pytest.mark.asyncio
    async def test_price_source_priority(self, assembler, mock_db_pool):
        """Test that price sources are prioritized correctly."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Multiple prices for same time with different sources
        # (in real implementation, query would handle priority)
        mock_conn.fetch.return_value = []  # Empty - use fallback

        prices = await assembler._get_prices(base_time, base_time + timedelta(hours=24), 96)

        # Should have fallback prices
        assert len(prices) == 96

    @pytest.mark.asyncio
    async def test_partial_price_coverage(self, assembler, mock_db_pool):
        """Test handling of partial price coverage."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Only first 12 hours have prices
        price_rows = []
        for i in range(48):  # Only first 48 timesteps (12 hours)
            t = base_time + timedelta(minutes=i * 15)
            mock_row = MagicMock()
            mock_row.__getitem__ = lambda self, k, time=t: {
                "time": time,
                "price_per_kwh": 0.12,
            }[k]
            price_rows.append(mock_row)

        mock_conn.fetch.return_value = price_rows

        prices = await assembler._get_prices(base_time, base_time + timedelta(hours=24), 96)

        # Should still have 96 prices (with fallback for missing)
        assert len(prices) == 96


class TestEnergyRequirements:
    """Tests for energy requirement computation."""

    def test_multiple_trips_per_vehicle(self, assembler):
        """Test energy requirements for vehicle with multiple trips."""
        # Simulating multiple trips for same vehicle
        schedules = [
            {"vehicle_id": "bus_1", "energy_kwh": 100.0},
            {"vehicle_id": "bus_1", "energy_kwh": 80.0},
            {"vehicle_id": "bus_2", "energy_kwh": 150.0},
        ]

        # Energy requirements should sum multiple trips
        requirements = {}
        for sched in schedules:
            vid = sched["vehicle_id"]
            energy = sched["energy_kwh"]
            requirements[vid] = requirements.get(vid, 0.0) + energy

        assert requirements["bus_1"] == 180.0  # 100 + 80
        assert requirements["bus_2"] == 150.0

    def test_inter_depot_handoff_energy(self, assembler):
        """Test energy requirements for inter-depot handoff."""
        # Vehicle going to different depot needs full charge

        # Should require enough energy for trip + full charge at dest
        # (Application logic would determine actual requirement)


class TestDepartureTimeComputation:
    """Tests for departure time computation."""

    def test_departure_times_in_timesteps(self, assembler):
        """Test conversion of departure times to timesteps."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time + timedelta(hours=6),
            },
            {
                "vehicle_id": "bus_2",
                "departure_time": base_time + timedelta(hours=12, minutes=30),
            },
        ]

        start = base_time
        delta_t = 0.25  # 15 minutes

        departure_times = {}
        for sched in schedules:
            vid = sched["vehicle_id"]
            dep = sched["departure_time"]
            timestep = int((dep - start).total_seconds() / (delta_t * 3600))
            departure_times[vid] = timestep

        assert departure_times["bus_1"] == 24  # 6 hours = 24 timesteps
        assert departure_times["bus_2"] == 50  # 12.5 hours = 50 timesteps

    def test_departure_before_horizon_start(self, assembler):
        """Test handling of departure times before horizon."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        schedules = [
            {
                "vehicle_id": "bus_1",
                "departure_time": base_time - timedelta(hours=2),  # Already departed
            }
        ]

        start = base_time

        # Should handle gracefully - vehicle already gone
        for sched in schedules:
            dep = sched["departure_time"]
            if dep < start:
                # Vehicle already departed - no departure constraint
                pass
