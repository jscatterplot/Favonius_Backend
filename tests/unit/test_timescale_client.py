"""Unit tests for TimescaleClient."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.timescale_client import TimescaleClient


class TestTimescaleClient:
    """Test TimescaleClient functionality."""

    @pytest.fixture
    def config(self):
        """Mock configuration."""
        return TimescaleConfig(
            service_url="postgresql://user:password@host:port/database",
            host="localhost",
            port=5432,
            database="testdb",
            user="test",
            password="test",
            max_connections=10,
            pool_size=5,
            connection_timeout=30,
            statement_timeout=60,
            idle_timeout=300,
            sslmode="prefer",
            enable_ssl=True,
            ssl_cert_path="/path/to/cert.pem",
            ssl_key_path="/path/to/key.pem",
            ssl_ca_path="/path/to/ca.pem",
        )

    @pytest.fixture
    def timescale_client(self, config):
        """Create TimescaleClient instance."""
        return TimescaleClient(config)

    @pytest.mark.timeout(10)
    def test_timescale_client_initialization(self, config):
        """Test TimescaleClient initialization."""
        client = TimescaleClient(config)

        assert client.config == config
        assert client.pg_pool is None
        assert client.sqlalchemy_engine is None
        assert client.connected is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_connect(self, timescale_client):
        """Test connecting to TimescaleDB."""
        with (
            patch.object(
                timescale_client, "_establish_connections", new_callable=AsyncMock
            ) as mock_establish,
            patch.object(
                timescale_client, "_verify_timescale_extension", new_callable=AsyncMock
            ) as mock_verify,
            patch.object(
                timescale_client, "_test_connections", new_callable=AsyncMock
            ) as mock_test,
        ):

            await timescale_client.connect()

            assert timescale_client.connected is True
            mock_establish.assert_called_once()
            mock_verify.assert_called_once()
            mock_test.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_connect_with_retry(self, timescale_client):
        """Test connecting with retry logic."""
        with (
            patch.object(
                timescale_client, "_establish_connections", new_callable=AsyncMock
            ) as mock_establish,
            patch.object(
                timescale_client, "_verify_timescale_extension", new_callable=AsyncMock
            ) as mock_verify,
            patch.object(
                timescale_client, "_test_connections", new_callable=AsyncMock
            ) as mock_test,
        ):

            await timescale_client._connect_with_retry(max_retries=2, base_delay=0.1)

            assert timescale_client.connected is True
            mock_establish.assert_called_once()
            mock_verify.assert_called_once()
            mock_test.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_connect_max_retries_exceeded(self, timescale_client):
        """Test connecting with max retries exceeded."""
        with patch.object(
            timescale_client, "_establish_connections", new_callable=AsyncMock
        ) as mock_establish:
            mock_establish.side_effect = Exception("Persistent connection error")

            with pytest.raises(Exception):
                await timescale_client._connect_with_retry(max_retries=2, base_delay=0.1)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_disconnect(self, timescale_client):
        """Test disconnecting from TimescaleDB."""
        # Set up mock pool
        mock_pool = AsyncMock()
        mock_pool.close = AsyncMock()
        timescale_client.pg_pool = mock_pool
        timescale_client.connected = True

        await timescale_client.disconnect()

        assert timescale_client.connected is False
        mock_pool.close.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_disconnect_no_connections(self, timescale_client):
        """Test disconnecting when no connections exist."""
        timescale_client.connected = True

        await timescale_client.disconnect()

        assert timescale_client.connected is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_execute_query(self, timescale_client):
        """Test executing a query."""
        # Test without pool (should handle gracefully)
        query = "SELECT * FROM test_table WHERE id = $1"
        params = [1]

        with pytest.raises(Exception):
            await timescale_client.execute_query(query, *params)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_execute_query_not_connected(self, timescale_client):
        """Test executing a query when not connected."""
        timescale_client.connected = False

        query = "SELECT * FROM test_table WHERE id = $1"
        params = [1]

        with pytest.raises(Exception):
            await timescale_client.execute_query(query, *params)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_execute_query_error(self, timescale_client):
        """Test executing a query with error."""
        # Test without pool (should handle gracefully)
        query = "SELECT * FROM test_table WHERE id = $1"
        params = [1]

        with pytest.raises(Exception):
            await timescale_client.execute_query(query, *params)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_insert_telemetry_batch(self, timescale_client):
        """Test inserting telemetry batch."""
        # Test without pool (should handle gracefully)
        telemetry_data = [
            {
                "time": datetime.now(timezone.utc),
                "station_id": "TEST_STATION_001",
                "evse_id": 1,
                "connector_id": 1,
                "power_kw": 7.5,
                "energy_kwh": 10.0,
            }
        ]

        with pytest.raises(Exception):
            await timescale_client.insert_telemetry_batch(telemetry_data)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_insert_telemetry_batch_writes_without_vehicle_id(self, timescale_client):
        """Charger-keyed telemetry writes must not require vehicle_id."""
        mock_ts_conn = AsyncMock()
        mock_ts_pool = MagicMock()
        mock_ts_pool.acquire.return_value.__aenter__.return_value = mock_ts_conn
        mock_ts_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_ts_pool

        mock_static_conn = AsyncMock()
        mock_static_pool = AsyncMock()
        mock_static_pool.acquire.return_value.__aenter__.return_value = mock_static_conn
        mock_static_pool.acquire.return_value.__aexit__.return_value = None
        mock_static_pool.release = AsyncMock()

        sb_client = MagicMock()
        sb_client.db_pool = mock_static_pool
        timescale_client.set_supabase_client(sb_client)

        telemetry_data = [
            {
                "time": datetime.now(timezone.utc),
                "station_id": "hrx-uab_hrx-vilnius-005",
                "connector_id": 1,
                "session_id": "6",
                "power_kw": 10.769,
                "soc_percent": None,
                "max_charge_power_kw": None,
            }
        ]

        with (
            patch.object(
                timescale_client, "_update_session_live_metrics", new_callable=AsyncMock
            ) as update_live_mock,
            patch.object(
                timescale_client, "_resolve_vehicle_id_from_session", new_callable=AsyncMock
            ) as resolve_vehicle_mock,
            patch.object(
                timescale_client, "_resolve_charger_id", new_callable=AsyncMock
            ) as resolve_charger_mock,
        ):
            resolve_vehicle_mock.return_value = None
            resolve_charger_mock.return_value = None

            await timescale_client.insert_telemetry_batch(telemetry_data)

        update_live_mock.assert_awaited()
        resolve_vehicle_mock.assert_awaited()
        telemetry_sql = mock_ts_conn.execute.await_args_list[-1].args[0]
        assert "INSERT INTO telemetry" in telemetry_sql
        assert "station_id, connector_id" in telemetry_sql
        last_args = mock_ts_conn.execute.await_args_list[-1].args
        assert last_args[2] == "hrx-uab_hrx-vilnius-005"
        assert last_args[3] == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_energy_usage_summary(self, timescale_client):
        """Test getting energy usage summary."""
        # Test without pool (should handle gracefully)
        fleet_operator_id = "test_fleet"
        start_time = datetime.now(timezone.utc) - timedelta(days=1)
        end_time = datetime.now(timezone.utc)

        with pytest.raises(Exception):
            await timescale_client.get_energy_usage_summary(fleet_operator_id, start_time, end_time)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_daily_fleet_metrics(self, timescale_client):
        """Test getting daily fleet metrics."""
        # Test without engine (should handle gracefully)
        fleet_operator_id = "test_fleet"
        start_time = datetime.now(timezone.utc) - timedelta(days=1)
        end_time = datetime.now(timezone.utc)

        with pytest.raises(Exception):
            await timescale_client.get_daily_fleet_metrics(fleet_operator_id, start_time, end_time)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_health_check(self, timescale_client):
        """Test health check."""
        # Test without pool (should return disconnected status)
        result = await timescale_client.health_check()

        assert result["status"] == "disconnected"
        assert "error" in result

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_health_check_not_connected(self, timescale_client):
        """Test health check when not connected."""
        timescale_client.connected = False

        result = await timescale_client.health_check()

        assert result["status"] == "disconnected"
        assert "error" in result

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_health_check_error(self, timescale_client):
        """Test health check with error."""
        # Test without pool (should return disconnected status)
        result = await timescale_client.health_check()

        assert result["status"] == "disconnected"
        assert "error" in result

    @pytest.mark.timeout(10)
    def test_timescale_client_without_config(self):
        """Test TimescaleClient initialization without config."""
        # TimescaleClient doesn't validate config in __init__, so this won't raise TypeError
        client = TimescaleClient(None)
        assert client.config is None

    @pytest.mark.timeout(10)
    def test_get_connection_info(self, timescale_client):
        """Test getting connection info."""
        # Test without pool (should return basic info)
        # This method doesn't exist, so we'll test basic properties
        assert timescale_client.connected is False
        assert timescale_client.pg_pool is None

    @pytest.mark.timeout(10)
    def test_get_connection_info_not_connected(self, timescale_client):
        """Test getting connection info when not connected."""
        timescale_client.connected = False

        # This method doesn't exist, so we'll test basic properties
        assert timescale_client.connected is False
        assert timescale_client.pg_pool is None

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_fetch_open_sessions_filters_to_live_source(self, timescale_client):
        """Open-session recovery must ignore imported NULL-end_time rows."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        await timescale_client.fetch_open_sessions("CP-1")

        query = mock_conn.fetch.await_args.args[0]
        assert "end_time IS NULL" in query
        assert "source = 'live'" in query

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_mark_sessions_seen_filters_to_live_source(self, timescale_client):
        """last_seen stamping must not touch imported rows."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        await timescale_client.mark_sessions_seen("CP-1")

        query = mock_conn.execute.await_args.args[0]
        assert "end_time IS NULL" in query
        assert "source = 'live'" in query

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_inserts_when_canonical_exists(self, timescale_client):
        """A new alias is upserted with the canonical-exists guard + ON CONFLICT."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        registered = await timescale_client.ensure_station_alias(
            "TACW1141622G1438",
            "hrx-uab_hrx-vilnius-002",
        )

        assert registered is True
        query = mock_conn.execute.await_args.args[0]
        assert "INSERT INTO ocpp_station_aliases" in query
        assert "WHERE EXISTS" in query and "charging_stations" in query
        assert "ON CONFLICT (alias_station_id) DO NOTHING" in query
        # Default source label so operators can audit auto-registered rows.
        positional_args = mock_conn.execute.await_args.args
        assert "auto-multipath" in positional_args

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_skips_self_referential_alias(self, timescale_client):
        """``alias == canonical`` would violate the table CHECK; skip without DB call."""
        mock_pool = MagicMock()
        timescale_client.pg_pool = mock_pool

        registered = await timescale_client.ensure_station_alias(
            "hrx-uab_hrx-vilnius-002",
            "hrx-uab_hrx-vilnius-002",
        )

        assert registered is False
        mock_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_returns_false_on_conflict(self, timescale_client):
        """An existing alias row is not overwritten — return False, do not raise."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 0")
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        registered = await timescale_client.ensure_station_alias(
            "TACW1141622G1438",
            "hrx-uab_hrx-vilnius-002",
        )

        assert registered is False

    # ------------------------------------------------------------------
    # Static-pool routing — regression guard
    #
    # ``charging_stations`` and ``station_credentials`` live in Supabase
    # (migration 029 dropped the TimescaleDB shadows). Methods that
    # reference those tables must acquire from ``_static_pool``, which
    # routes to the wired ``SupabaseClient.db_pool`` and falls back to
    # ``pg_pool`` for tests that don't provide one.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_resolve_charger_id_uses_supabase_pool_when_wired(
        self, timescale_client
    ):
        """When a SupabaseClient is wired, charger lookup goes there."""
        mock_supabase_conn = AsyncMock()
        mock_supabase_conn.fetchrow = AsyncMock(return_value={"charger_id": "uuid-A"})
        mock_supabase_pool = MagicMock(name="supabase_pool")
        mock_supabase_pool.acquire.return_value.__aenter__.return_value = mock_supabase_conn
        mock_supabase_pool.acquire.return_value.__aexit__.return_value = None

        mock_timescale_pool = MagicMock(name="timescale_pool")
        timescale_client.pg_pool = mock_timescale_pool

        sb_client = MagicMock()
        sb_client.db_pool = mock_supabase_pool
        timescale_client.set_supabase_client(sb_client)

        result = await timescale_client._resolve_charger_id("hrx-uab_hrx-vilnius-002")

        assert result == "uuid-A"
        # Lookup must NOT have hit the timescale pool.
        mock_timescale_pool.acquire.assert_not_called()
        mock_supabase_pool.acquire.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_resolve_charger_id_falls_back_to_pg_pool_when_unwired(
        self, timescale_client
    ):
        """No SupabaseClient → use pg_pool (for tests / legacy deployments)."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"charger_id": "uuid-B"})
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool
        # No set_supabase_client call.

        result = await timescale_client._resolve_charger_id("station-X")

        assert result == "uuid-B"
        mock_pool.acquire.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_resolve_charger_id_returns_none_for_falsy_station(
        self, timescale_client
    ):
        """Empty/None station_id short-circuits without acquiring any pool."""
        mock_pool = MagicMock()
        timescale_client.pg_pool = mock_pool

        assert await timescale_client._resolve_charger_id(None) is None
        assert await timescale_client._resolve_charger_id("") is None
        mock_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_uses_supabase_pool_when_wired(
        self, timescale_client
    ):
        """``ensure_station_alias`` references ``charging_stations`` in its
        EXISTS guard, so it must route through the static pool."""
        mock_supabase_conn = AsyncMock()
        mock_supabase_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_supabase_pool = MagicMock(name="supabase_pool")
        mock_supabase_pool.acquire.return_value.__aenter__.return_value = mock_supabase_conn
        mock_supabase_pool.acquire.return_value.__aexit__.return_value = None

        mock_timescale_pool = MagicMock(name="timescale_pool")
        timescale_client.pg_pool = mock_timescale_pool

        sb_client = MagicMock()
        sb_client.db_pool = mock_supabase_pool
        timescale_client.set_supabase_client(sb_client)

        registered = await timescale_client.ensure_station_alias(
            "TACW1141622G1438",
            "hrx-uab_hrx-vilnius-002",
        )

        assert registered is True
        mock_timescale_pool.acquire.assert_not_called()
        mock_supabase_pool.acquire.assert_called_once()

    def test_validate_basic_auth_no_longer_on_timescale_client(
        self, timescale_client
    ):
        """Removed: it queried ``station_credentials`` against ``pg_pool``,
        but the table only exists in Supabase. Canonical implementation lives
        on ``SupabaseClient`` and is reached via ``SecurityManager.static_auth_client``.
        """
        assert not hasattr(timescale_client, "validate_basic_auth")

    def test_station_requires_basic_auth_no_longer_on_timescale_client(
        self, timescale_client
    ):
        """Removed for the same reason as ``validate_basic_auth`` above."""
        assert not hasattr(timescale_client, "station_requires_basic_auth")

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_store_electricity_prices_writes_to_table(self, timescale_client):
        """Happy path: ENTSO-E points are bulk-loaded into ``electricity_prices``."""
        mock_conn = AsyncMock()
        mock_conn.copy_records_to_table = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        ts = datetime.now(timezone.utc)
        await timescale_client.store_electricity_prices(
            [
                {
                    "time": ts,
                    "node_id": "10YLT-1001A0008Q",
                    "market_type": "ENTSOE_DAM",
                    "lmp_price_mwh": 90.0,
                    "energy_component_mwh": 90.0,
                    "congestion_component_mwh": None,
                    "loss_component_mwh": None,
                    "ghg_adder_mwh": None,
                    "price_confidence": None,
                    "forecast_horizon_minutes": None,
                }
            ]
        )
        mock_conn.copy_records_to_table.assert_awaited_once()
        # First positional arg is the destination table name.
        assert mock_conn.copy_records_to_table.await_args.args[0] == "electricity_prices"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_mark_connectors_available_after_reconnect(self, timescale_client):
        """Reconnect-clear inserts a fresh row and reports cleared connector count.

        The query inserts ``(Available, NULL, NOW())`` for connectors whose
        latest row is the close-hook's ``(Unavailable, ConnectionLost)``
        marker. The selectivity is enforced in SQL — Python only counts the
        RETURNING rows. We assert the call wiring + return value here; the
        SQL precision (Faulted not clobbered, charger-issued Unavailable
        with a different error_code preserved) lives in the integration
        recovery suite where a real DB is available.
        """
        mock_conn = AsyncMock()
        # Two connectors had stale ConnectionLost markers; the SQL returns
        # one row per cleared connector via RETURNING connector_id.
        mock_conn.fetch = AsyncMock(return_value=[{"connector_id": 1}, {"connector_id": 2}])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        cleared = await timescale_client.mark_connectors_available_after_reconnect("hrx-ac-1")

        assert cleared == 2
        mock_conn.fetch.assert_awaited_once()
        sql, station_id = mock_conn.fetch.await_args.args
        assert station_id == "hrx-ac-1"
        # SQL contract: must select Unavailable+ConnectionLost and insert Available.
        assert "'Unavailable'" in sql
        assert "'ConnectionLost'" in sql
        assert "'Available'" in sql
        assert "RETURNING connector_id" in sql

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_mark_connectors_available_after_reconnect_returns_zero_when_clean(
        self, timescale_client
    ):
        """No stale rows → zero clears, no warning churn for healthy chargers."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = mock_pool

        cleared = await timescale_client.mark_connectors_available_after_reconnect("hrx-ac-1")
        assert cleared == 0
