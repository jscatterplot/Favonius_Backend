"""Unit tests for StateAssembler.

Reference: PRD.md#11-2-unit-test-requirements
Coverage target: ≥ 80%
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg

from src.core.models import DepotConfig
from src.core.state.assembler import StateAssembler


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    return pool


@pytest.fixture
def depot_config():
    """Depot configuration for testing."""
    return DepotConfig(
        vehicle_capacities={'bus_1': 324.0, 'bus_2': 324.0, 'bus_3': 200.0},
        charger_power=80.0,
        charger_efficiency=0.95,
        n_chargers=5,
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
def assembler(mock_db_pool, depot_id, depot_config):
    """StateAssembler instance for testing."""
    return StateAssembler(mock_db_pool, depot_id, depot_config)


class TestStateAssemblerInitialization:
    """Test StateAssembler initialization."""

    def test_init(self, mock_db_pool, depot_id, depot_config):
        """Test StateAssembler initialization."""
        assembler = StateAssembler(mock_db_pool, depot_id, depot_config)
        assert assembler.pool == mock_db_pool
        assert assembler.depot_id == depot_id
        assert assembler.config == depot_config

    def test_init_with_uuid(self, mock_db_pool, depot_config):
        """Test initialization with UUID object."""
        depot_uuid = uuid4()
        assembler = StateAssembler(mock_db_pool, depot_uuid, depot_config)
        assert assembler.depot_id == str(depot_uuid)


class TestGetVehicleSocs:
    """Test _get_vehicle_socs method."""

    @pytest.mark.asyncio
    async def test_get_vehicle_socs_single(self, assembler, mock_db_pool):
        """Test retrieving SoC for single vehicle."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        mock_row = MagicMock()
        mock_row.__getitem__.side_effect = lambda k: {
            'vehicle_id': 'bus_1',
            'soc': 0.65
        }[k]
        mock_conn.fetch.return_value = [mock_row]

        socs = await assembler._get_vehicle_socs()

        assert 'bus_1' in socs
        assert socs['bus_1'] == 0.65
        mock_conn.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_vehicle_socs_multiple(self, assembler, mock_db_pool):
        """Test retrieving SoC for multiple vehicles."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        rows = [
            MagicMock(**{'__getitem__.side_effect': lambda k: {
                'vehicle_id': 'bus_1',
                'soc': 0.65
            }[k]}),
            MagicMock(**{'__getitem__.side_effect': lambda k: {
                'vehicle_id': 'bus_2',
                'soc': 0.80
            }[k]}),
        ]
        mock_conn.fetch.return_value = rows

        socs = await assembler._get_vehicle_socs()

        assert len(socs) == 2
        assert socs['bus_1'] == 0.65
        assert socs['bus_2'] == 0.80

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
        
        # Mock 6 hours of hourly prices
        rows = []
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        for i in range(6):
            row = MagicMock()
            row.__getitem__.side_effect = lambda k, t=base_time + timedelta(hours=i), p=0.10 + i*0.01: {
                'time': t,
                'price_per_kwh': p
            }[k]
            rows.append(row)
        mock_conn.fetch.return_value = rows

        start = base_time
        end = start + timedelta(hours=24)
        n_steps = 96  # 24 hours * 4 timesteps/hour

        prices = await assembler._get_prices(start, end, n_steps)

        # Should have 96 prices (24 hours * 4)
        assert len(prices) == 96
        # First 4 should be same (first hour repeated)
        assert prices[0] == prices[1] == prices[2] == prices[3]
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
        rows = [
            MagicMock(**{'__getitem__.side_effect': lambda k, t=base_time, p=0.10: {
                'time': t,
                'price_per_kwh': p
            }[k]}),
            MagicMock(**{'__getitem__.side_effect': lambda k, t=base_time + timedelta(hours=1), p=0.20: {
                'time': t,
                'price_per_kwh': p
            }[k]}),
        ]
        mock_conn.fetch.return_value = rows

        start = base_time
        end = start + timedelta(hours=2)
        n_steps = 8  # 2 hours * 4 timesteps/hour

        prices = await assembler._get_prices(start, end, n_steps)

        assert len(prices) == 8
        # First 4 should be 0.10, next 4 should be 0.20
        assert all(p == 0.10 for p in prices[:4])
        assert all(p == 0.20 for p in prices[4:8])


class TestGetSchedules:
    """Test _get_schedules method."""

    @pytest.mark.asyncio
    async def test_get_schedules_single(self, assembler, mock_db_pool):
        """Test retrieving single schedule."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        mock_row = {
            'vehicle_id': 'bus_1',
            'departure_time': base_time + timedelta(hours=6),
            'return_time': base_time + timedelta(hours=10),
            'estimated_energy_kwh': 150.0,
            'route_id': 'route_1',
        }
        mock_conn.fetch.return_value = [mock_row]

        start = base_time
        end = start + timedelta(hours=24)
        schedules = await assembler._get_schedules(start, end)

        assert len(schedules) == 1
        assert schedules[0]['vehicle_id'] == 'bus_1'
        assert schedules[0]['estimated_energy_kwh'] == 150.0

    @pytest.mark.asyncio
    async def test_get_schedules_multiple(self, assembler, mock_db_pool):
        """Test retrieving multiple schedules."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        rows = [
            {
                'vehicle_id': 'bus_1',
                'departure_time': base_time + timedelta(hours=6),
                'return_time': base_time + timedelta(hours=10),
                'estimated_energy_kwh': 150.0,
                'route_id': 'route_1',
            },
            {
                'vehicle_id': 'bus_2',
                'departure_time': base_time + timedelta(hours=8),
                'return_time': base_time + timedelta(hours=12),
                'estimated_energy_kwh': 200.0,
                'route_id': 'route_2',
            },
        ]
        mock_conn.fetch.return_value = rows

        start = base_time
        end = start + timedelta(hours=24)
        schedules = await assembler._get_schedules(start, end)

        assert len(schedules) == 2
        assert schedules[0]['vehicle_id'] == 'bus_1'
        assert schedules[1]['vehicle_id'] == 'bus_2'

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
                'vehicle_id': 'bus_1',
                'departure_time': base_time + timedelta(hours=6),
                'return_time': base_time + timedelta(hours=10),
            }
        ]
        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # bus_1 should be unavailable during trip (timesteps 24-40, 6-10 hours)
        # bus_2 and bus_3 should be available
        assert availability['bus_2'][0] is True
        assert availability['bus_3'][0] is True
        
        # Check bus_1 is unavailable during trip
        trip_start_idx = 24  # 6 hours * 4 timesteps/hour
        trip_end_idx = 40    # 10 hours * 4 timesteps/hour
        assert any(not availability['bus_1'][i] for i in range(trip_start_idx, trip_end_idx))

    def test_compute_availability_unknown_vehicle(self, assembler):
        """Test availability with schedule for unknown vehicle."""
        base_time = datetime.utcnow()
        schedules = [
            {
                'vehicle_id': 'unknown_bus',
                'departure_time': base_time + timedelta(hours=6),
                'return_time': base_time + timedelta(hours=10),
            }
        ]
        start = base_time
        n_steps = 96

        availability = assembler._compute_availability(schedules, start, n_steps)

        # Unknown vehicle should be ignored
        assert 'unknown_bus' not in availability


class TestComputeDepartureTimes:
    """Test _compute_departure_times method."""

    def test_compute_departure_times_single(self, assembler):
        """Test departure time computation for single vehicle."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                'vehicle_id': 'bus_1',
                'departure_time': base_time + timedelta(hours=6),
                'return_time': base_time + timedelta(hours=10),
            }
        ]
        start = base_time

        departures = assembler._compute_departure_times(schedules, start)

        assert 'bus_1' in departures
        assert departures['bus_1'] == 24  # 6 hours * 4 timesteps/hour

    def test_compute_departure_times_multiple(self, assembler):
        """Test departure time computation for multiple vehicles."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                'vehicle_id': 'bus_1',
                'departure_time': base_time + timedelta(hours=6),
                'return_time': base_time + timedelta(hours=10),
            },
            {
                'vehicle_id': 'bus_2',
                'departure_time': base_time + timedelta(hours=8),
                'return_time': base_time + timedelta(hours=12),
            },
        ]
        start = base_time

        departures = assembler._compute_departure_times(schedules, start)

        assert len(departures) == 2
        assert departures['bus_1'] == 24  # 6 hours
        assert departures['bus_2'] == 32  # 8 hours

    def test_compute_departure_times_earliest(self, assembler):
        """Test that earliest departure is kept for each vehicle."""
        base_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        schedules = [
            {
                'vehicle_id': 'bus_1',
                'departure_time': base_time + timedelta(hours=8),
                'return_time': base_time + timedelta(hours=10),
            },
            {
                'vehicle_id': 'bus_1',  # Same vehicle, earlier departure
                'departure_time': base_time + timedelta(hours=6),
                'return_time': base_time + timedelta(hours=10),
            },
        ]
        start = base_time

        departures = assembler._compute_departure_times(schedules, start)

        # Should keep earliest departure (6 hours = timestep 24)
        assert departures['bus_1'] == 24


class TestComputeEnergyRequirements:
    """Test _compute_energy_requirements method."""

    def test_compute_energy_requirements_with_data(self, assembler):
        """Test energy requirement computation with schedule data."""
        schedules = [
            {
                'vehicle_id': 'bus_1',
                'estimated_energy_kwh': 150.0,
            },
            {
                'vehicle_id': 'bus_2',
                'estimated_energy_kwh': 200.0,
            },
        ]

        requirements = assembler._compute_energy_requirements(schedules)

        assert requirements['bus_1'] == 150.0
        assert requirements['bus_2'] == 200.0

    def test_compute_energy_requirements_default(self, assembler):
        """Test energy requirement computation with missing data."""
        schedules = [
            {
                'vehicle_id': 'bus_1',
                # No estimated_energy_kwh
            },
        ]

        requirements = assembler._compute_energy_requirements(schedules)

        assert requirements['bus_1'] == 100.0  # Default value

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
        mock_row.__getitem__.side_effect = lambda k: {
            'peak': 250.0
        }[k]
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
        mock_row.__getitem__.side_effect = lambda k: {
            'peak': None
        }[k]
        mock_conn.fetchrow.return_value = mock_row

        peak = await assembler._get_current_month_peak()

        assert peak == 0.0


class TestGetDemandChargeRate:
    """Test _get_demand_charge_rate method."""

    @pytest.mark.asyncio
    async def test_get_demand_charge_rate_mvp(self, assembler):
        """Test demand charge rate returns fixed value for MVP."""
        rate = await assembler._get_demand_charge_rate()
        assert rate == 20.0  # MVP hardcoded $20/kW


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


class TestGetCurrentState:
    """Test get_current_state integration method."""

    @pytest.mark.asyncio
    async def test_get_current_state_full(self, assembler, mock_db_pool):
        """Test full state assembly."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        # Mock vehicle SoCs
        mock_conn.fetch.side_effect = [
            [  # _get_vehicle_socs
                MagicMock(**{'__getitem__.side_effect': lambda k: {
                    'vehicle_id': 'bus_1',
                    'soc': 0.65
                }[k]}),
            ],
            [  # _get_prices
                MagicMock(**{'__getitem__.side_effect': lambda k, t=datetime.utcnow(), p=0.10: {
                    'time': t,
                    'price_per_kwh': p
                }[k]}),
            ],
            [],  # _get_schedules
        ]
        
        # Mock current month peak
        mock_peak_row = MagicMock()
        mock_peak_row.__getitem__.side_effect = lambda k: {'peak': 100.0}[k]
        mock_conn.fetchrow.return_value = mock_peak_row

        state = await assembler.get_current_state(horizon_hours=24)

        assert state.vehicle_socs['bus_1'] == 0.65
        assert len(state.prices) == 96
        assert state.current_month_peak == 100.0
        assert state.battery_soc == 0.5  # MVP default
        assert state.demand_charge_rate == 20.0  # MVP default
        assert all(p == 0.0 for p in state.building_power)  # MVP default

    @pytest.mark.asyncio
    async def test_get_current_state_custom_horizon(self, assembler, mock_db_pool):
        """Test state assembly with custom horizon."""
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        mock_conn.fetch.side_effect = [
            [],  # _get_vehicle_socs
            [],  # _get_prices
            [],  # _get_schedules
        ]
        
        mock_conn.fetchrow.return_value = None

        state = await assembler.get_current_state(horizon_hours=12)

        # 12 hours = 48 timesteps (12 * 4)
        assert len(state.prices) == 48
        assert len(state.building_power) == 48

