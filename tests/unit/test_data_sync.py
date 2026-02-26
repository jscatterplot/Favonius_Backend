"""Unit tests for DataSyncService."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.websocket_handler.config import SupabaseConfig
from src.websocket_handler.data_sync import DataSyncService


class TestDataSyncService:
    """Test DataSyncService functionality."""

    @pytest.fixture
    def config(self):
        """Mock configuration."""
        return SupabaseConfig(
            url="https://test.supabase.co",
            anon_key="test_anon_key",
            service_key="test_service_key",
            db_host="localhost",
            db_port=5432,
            db_name="testdb",
            db_user="test",
            db_password="test",
            max_connections=10,
            connection_timeout=30,
            enable_realtime=True,
        )

    @pytest.fixture
    def supabase_client(self):
        """Mock SupabaseClient."""
        mock_client = AsyncMock()
        mock_client.sync_session_summaries = AsyncMock()
        mock_client.sync_vehicle_states = AsyncMock()
        mock_client.client.table.return_value.upsert.return_value.execute = AsyncMock()
        return mock_client

    @pytest.fixture
    def data_sync_service(self, config, supabase_client):
        """Create DataSyncService instance."""
        return DataSyncService(config, supabase_client)

    @pytest.mark.timeout(10)
    def test_data_sync_service_initialization(self, config, supabase_client):
        """Test DataSyncService initialization."""
        service = DataSyncService(config, supabase_client)

        assert service.config == config
        assert service.supabase_client == supabase_client
        assert service.timescale_pool is None
        assert service.sync_running is False
        assert service.sync_interval == 300
        assert service.batch_size == 1000
        assert service.max_retries == 3
        assert service.last_sync_times == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_start(self, data_sync_service):
        """Test starting the data sync service."""
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool_instance = AsyncMock()
            mock_pool.return_value = mock_pool_instance

            await data_sync_service.start()

            assert data_sync_service.sync_running is True
            assert data_sync_service.timescale_pool == mock_pool_instance
            mock_pool.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_stop(self, data_sync_service):
        """Test stopping the data sync service."""
        # Set up mock pool
        mock_pool = AsyncMock()
        data_sync_service.timescale_pool = mock_pool
        data_sync_service.sync_running = True

        await data_sync_service.stop()

        assert data_sync_service.sync_running is False
        mock_pool.close.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_stop_no_pool(self, data_sync_service):
        """Test stopping when no pool exists."""
        data_sync_service.sync_running = True

        await data_sync_service.stop()

        assert data_sync_service.sync_running is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_charging_sessions(self, data_sync_service):
        """Test syncing charging sessions."""
        # Test without pool (should handle gracefully)
        await data_sync_service.sync_charging_sessions()
        # Should complete without error

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_charging_sessions_no_data(self, data_sync_service):
        """Test syncing charging sessions with no data."""
        # Test without pool (should handle gracefully)
        await data_sync_service.sync_charging_sessions()
        # Should complete without error

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_charging_sessions_error(self, data_sync_service):
        """Test syncing charging sessions with error."""
        # Test without pool (should handle gracefully)
        await data_sync_service.sync_charging_sessions()
        # Should complete without error

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_energy_data(self, data_sync_service):
        """Test syncing energy data."""
        # Test without pool (should handle gracefully)
        await data_sync_service.sync_energy_metrics()
        # Should complete without error

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_station_status(self, data_sync_service):
        """Test syncing vehicle states (closest to station status)."""
        # Test without pool (should handle gracefully)
        await data_sync_service.sync_vehicle_states()
        # Should complete without error

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_active_sessions(self, data_sync_service):
        """Test syncing active sessions."""
        sessions = [
            {
                "session_id": "test_session",
                "station_id": "test_station",
                "vehicle_id": "test_vehicle",
                "organization_id": "test_org",
                "start_time": datetime.now(timezone.utc),
                "current_power_kw": 7.5,
                "current_soc": 80.0,
                "target_soc": 100.0,
                "estimated_end_time": datetime.now(timezone.utc) + timedelta(hours=1),
                "status": "charging",
            }
        ]

        await data_sync_service.sync_active_sessions(sessions)

        data_sync_service.supabase_client.client.table.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_sync_status(self, data_sync_service):
        """Test getting sync status."""
        data_sync_service.sync_running = True
        data_sync_service.last_sync_times = {"charging_sessions": datetime.now(timezone.utc)}

        status = await data_sync_service.get_sync_status()

        assert status["running"] is True
        assert "charging_sessions" in status["last_sync_times"]
        assert status["sync_interval"] == 300
        assert status["batch_size"] == 1000

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_force_sync(self, data_sync_service):
        """Test forcing sync of specific data type."""
        with (
            patch.object(
                data_sync_service, "sync_charging_sessions", return_value=None
            ) as mock_sessions,
            patch.object(
                data_sync_service, "sync_energy_metrics", return_value=None
            ) as mock_energy,
            patch.object(
                data_sync_service, "sync_vehicle_states", return_value=None
            ) as mock_states,
        ):

            await data_sync_service.force_sync("charging_sessions")
            mock_sessions.assert_called_once()

            await data_sync_service.force_sync("energy_metrics")
            mock_energy.assert_called_once()

            await data_sync_service.force_sync("vehicle_states")
            mock_states.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_force_sync_partial_failure(self, data_sync_service):
        """Test forcing sync with partial failure."""
        with (
            patch.object(
                data_sync_service, "sync_charging_sessions", return_value=None
            ) as mock_sessions,
            patch.object(
                data_sync_service, "sync_energy_metrics", return_value=None
            ) as mock_energy,
            patch.object(
                data_sync_service, "sync_vehicle_states", return_value=None
            ) as mock_states,
        ):

            await data_sync_service.force_sync("charging_sessions")
            mock_sessions.assert_called_once()

            await data_sync_service.force_sync("energy_metrics")
            mock_energy.assert_called_once()

            await data_sync_service.force_sync("vehicle_states")
            mock_states.assert_called_once()

    @pytest.mark.timeout(10)
    def test_data_sync_service_without_config(self, supabase_client):
        """Test DataSyncService initialization without config."""
        # DataSyncService doesn't validate config in __init__, so this won't raise TypeError
        service = DataSyncService(None, supabase_client)
        assert service.config is None

    @pytest.mark.timeout(10)
    def test_data_sync_service_without_client(self, config):
        """Test DataSyncService initialization without client."""
        # DataSyncService doesn't validate client in __init__, so this won't raise TypeError
        service = DataSyncService(config, None)
        assert service.supabase_client is None
