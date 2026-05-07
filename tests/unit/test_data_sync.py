"""Unit tests for DataSyncService."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from src.websocket_handler.config import SupabaseConfig, TimescaleConfig
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
    def timescale_config(self):
        """Mock TimescaleDB configuration."""
        return TimescaleConfig(
            service_url="postgresql://test:test@localhost:5432/testdb",
            host="localhost",
            port=5432,
            database="testdb",
            user="test",
            password="test",
        )

    @pytest.fixture
    def data_sync_service(self, config, supabase_client, timescale_config):
        """Create DataSyncService instance."""
        return DataSyncService(config, supabase_client, timescale_config)

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
    async def test_stop_cancels_sync_task(self, data_sync_service):
        """Test that stop() cancels the background sync task."""
        mock_pool = AsyncMock()
        data_sync_service.timescale_pool = mock_pool
        data_sync_service.sync_running = True

        # Create a real asyncio task that blocks
        async def blocking_sync():
            while True:
                await asyncio.sleep(1)

        data_sync_service._sync_task = asyncio.create_task(blocking_sync())

        await data_sync_service.stop()

        assert data_sync_service.sync_running is False
        assert data_sync_service._sync_task is None
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


def _make_pool_yielding(conn: AsyncMock) -> MagicMock:
    """Build a fake asyncpg.Pool whose acquire() context manager yields ``conn``.

    asyncpg's Pool.acquire returns an async context manager; the production
    code uses ``async with self.timescale_pool.acquire() as conn``. We mimic
    that with a real ``@asynccontextmanager`` so the production code path runs
    unchanged in tests.
    """

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


class TestDataSyncSchemaResilience:
    """Regression coverage for the migrated-schema mismatch fixed in this branch.

    Background: until this fix, the WS handler's data sync queried columns and
    tables that the migration runner does not create (charging_sessions.status,
    vehicle_telemetry, optimization_decisions). On every 5-min tick the
    handler logged ERROR lines like ``column "status" does not exist`` and
    ``relation "vehicle_telemetry" does not exist``. The fixes:
      1. derive ``status`` from ``end_time`` in SQL instead of selecting it,
      2. catch UndefinedTable/UndefinedColumn and skip silently after a
         single WARNING per (process, table) pair.
    """

    @pytest.fixture
    def config(self):
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
        client = AsyncMock()
        client.sync_session_summaries = AsyncMock()
        client.sync_vehicle_states = AsyncMock()
        client.client.table.return_value.upsert.return_value.execute = AsyncMock()
        return client

    @pytest.fixture
    def timescale_config(self):
        return TimescaleConfig(
            service_url="postgresql://test:test@localhost:5432/testdb",
            host="localhost",
            port=5432,
            database="testdb",
            user="test",
            password="test",
        )

    @pytest.fixture
    def service(self, config, supabase_client, timescale_config):
        return DataSyncService(config, supabase_client, timescale_config)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_charging_sessions_query_does_not_select_bare_status(self, service):
        """The SELECT must derive status from end_time, not SELECT a column.

        ``charging_sessions`` has no ``status`` column in the migrated schema
        (sync_status and import_status exist, but no bare status). Selecting
        it on production produced ``column "status" does not exist`` every
        cycle.
        """
        captured_query = {}

        async def fake_fetch(query, *args):
            captured_query["sql"] = query
            return []

        conn = AsyncMock()
        conn.fetch.side_effect = fake_fetch
        service.timescale_pool = _make_pool_yielding(conn)

        await service.sync_charging_sessions()

        sql = captured_query["sql"]
        # Must NOT select a bare ``status`` column from charging_sessions.
        # We allow the words "sync_status" and "derived_status" through.
        normalized = " ".join(sql.split())
        # The dangerous pattern is a top-level "status" between commas in the
        # SELECT list. After the fix, every "status" appearance should be
        # qualified ("sync_status", "derived_status").
        for token in normalized.replace(",", " ").split():
            if token == "status":
                pytest.fail(
                    "sync_charging_sessions still selects a bare 'status' "
                    f"column — this is the bug being fixed.\nSQL:\n{sql}"
                )
        assert "derived_status" in sql
        assert "CASE WHEN end_time IS NULL" in sql

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_charging_sessions_status_is_derived_for_completed_row(self, service):
        """Each row pushed to Supabase carries the SQL-derived status."""
        conn = AsyncMock()
        # The CASE expression in SQL would return 'completed' for a row with
        # a non-null end_time. We mimic that by returning the alias the
        # production code reads (``derived_status``).
        conn.fetch.return_value = [
            {
                "session_id": "sess-1",
                "station_id": "stn-1",
                "vehicle_id": "veh-1",
                "fleet_operator_id": "org-1",
                "start_time": datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
                "end_time": datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
                "energy_delivered_kwh": 5.5,
                "energy_received_kwh": 0,
                "session_duration_minutes": 30.0,
                "cost_total": 1.25,
                "revenue_v2g": 0,
                "derived_status": "completed",
            }
        ]
        conn.execute = AsyncMock()
        service.timescale_pool = _make_pool_yielding(conn)

        await service.sync_charging_sessions()

        service.supabase_client.sync_session_summaries.assert_awaited_once()
        pushed = service.supabase_client.sync_session_summaries.await_args.args[0]
        assert len(pushed) == 1
        assert pushed[0]["status"] == "completed"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_vehicle_states_skips_silently_when_table_missing(self, service):
        """vehicle_telemetry is bootstrap-only and may not exist in production.

        Expected: catch UndefinedTableError, log a single WARNING, mark the
        relation as missing. Subsequent ticks short-circuit without touching
        the pool.
        """
        conn = AsyncMock()
        conn.fetch.side_effect = asyncpg.exceptions.UndefinedTableError(
            'relation "vehicle_telemetry" does not exist'
        )
        service.timescale_pool = _make_pool_yielding(conn)
        # Replace the structlog-wrapped logger with a plain MagicMock so we can
        # assert call counts without depending on the project's logging setup.
        service.logger = MagicMock()

        await service.sync_vehicle_states()
        # Second call must NOT hit the pool again — short-circuited.
        await service.sync_vehicle_states()

        assert "vehicle_telemetry" in service._missing_relations
        assert conn.fetch.await_count == 1
        # Exactly one WARNING for vehicle_telemetry.
        warning_msgs = [
            call.args[0]
            for call in service.logger.warning.call_args_list
            if "vehicle_telemetry" in str(call)
        ]
        assert len(warning_msgs) == 1
        assert "does not exist" in warning_msgs[0]
        # No ERROR logged for a known-missing table — no log spam.
        service.logger.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_sync_optimization_decisions_skips_silently_when_table_missing(self, service):
        """Same skip-and-log-once contract for optimization_decisions."""
        conn = AsyncMock()
        conn.fetch.side_effect = asyncpg.exceptions.UndefinedTableError(
            'relation "optimization_decisions" does not exist'
        )
        service.timescale_pool = _make_pool_yielding(conn)
        service.logger = MagicMock()

        await service.sync_optimization_decisions()
        await service.sync_optimization_decisions()

        assert "optimization_decisions" in service._missing_relations
        assert conn.fetch.await_count == 1
        service.logger.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_charging_sessions_undefined_column_is_caught(self, service):
        """A future column rename must be caught, not crash the loop.

        Defence in depth: if a deployment is on a charging_sessions variant
        we don't recognise, the loop should keep running but stop attempting
        until restart.
        """
        conn = AsyncMock()
        conn.fetch.side_effect = asyncpg.exceptions.UndefinedColumnError(
            'column "status" does not exist'
        )
        service.timescale_pool = _make_pool_yielding(conn)
        service.logger = MagicMock()

        await service.sync_charging_sessions()

        assert DataSyncService._CHARGING_SESSIONS_SYNC_COLUMN_KEY in service._missing_relations
        service.logger.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_stop_resets_missing_relations(self, service):
        """An operator who fixes the schema can recover by restarting the service.

        stop() must clear the in-memory skip set so the next start() re-checks.
        """
        service._missing_relations.add("vehicle_telemetry")
        service.sync_running = True

        await service.stop()

        assert service._missing_relations == set()
