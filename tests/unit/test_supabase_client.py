"""Unit tests for SupabaseClient static OCPP auth helpers."""

from unittest.mock import AsyncMock, MagicMock

import bcrypt
import pytest

from src.websocket_handler.config import SupabaseConfig
from src.websocket_handler.supabase_client import SupabaseClient


class TestSupabaseClient:
    """Test Supabase-backed static data helpers."""

    @pytest.fixture
    def config(self):
        """Create a minimal Supabase config."""
        return SupabaseConfig(
            url="https://example.supabase.co",
            anon_key="anon",
            service_key="service",
            db_host="localhost",
            db_port=5432,
            db_name="postgres",
            db_user="postgres",
            db_password="postgres",
            max_connections=5,
            connection_timeout=30,
        )

    @pytest.fixture
    def supabase_client(self, config):
        """Create a SupabaseClient instance."""
        return SupabaseClient(config)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_resolve_station_id_uses_active_alias(self, supabase_client):
        """Path identities resolve through Supabase alias config."""
        supabase_client.fetch_one = AsyncMock(
            return_value={"canonical_station_id": "pilot-depot-001"}
        )

        result = await supabase_client.resolve_station_id("TACW1000000G0001")

        assert result == "pilot-depot-001"
        supabase_client.fetch_one.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_inserts_row_when_canonical_exists(self, supabase_client):
        """A new alias is registered when the canonical row exists in charging_stations."""
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        supabase_client.db_pool = pool

        registered = await supabase_client.ensure_station_alias(
            "TACW1000000G0002",
            "pilot-depot-002",
        )

        assert registered is True
        query = conn.execute.await_args.args[0]
        assert "INSERT INTO ocpp_station_aliases" in query
        # WHERE EXISTS gate ensures we don't invent canonical ids the
        # operator never provisioned.
        assert "WHERE EXISTS" in query and "charging_stations" in query
        # Idempotent: re-runs from the same charger must not error.
        assert "ON CONFLICT (alias_station_id) DO NOTHING" in query

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_returns_false_on_conflict(self, supabase_client):
        """An existing alias row is not overwritten — return False, do not raise."""
        conn = AsyncMock()
        # ON CONFLICT DO NOTHING returns "INSERT 0 0" when the row already exists.
        conn.execute = AsyncMock(return_value="INSERT 0 0")
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        supabase_client.db_pool = pool

        registered = await supabase_client.ensure_station_alias(
            "TACW1000000G0002",
            "pilot-depot-002",
        )

        assert registered is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_skips_self_referential_alias(self, supabase_client):
        """``alias == canonical`` would violate the table CHECK; skip without DB call."""
        supabase_client.db_pool = MagicMock()

        registered = await supabase_client.ensure_station_alias(
            "pilot-depot-002",
            "pilot-depot-002",
        )

        assert registered is False
        supabase_client.db_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ensure_station_alias_returns_false_when_pool_unset(self, supabase_client):
        """Without a DB pool the helper is a no-op rather than crashing."""
        supabase_client.db_pool = None

        registered = await supabase_client.ensure_station_alias(
            "TACW1000000G0002",
            "pilot-depot-002",
        )

        assert registered is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_basic_auth_prefers_alias_credential(self, supabase_client):
        """Alias logins validate against the alias row when both credentials exist."""
        alias_password_hash = bcrypt.hashpw(
            b"alias-password",
            bcrypt.gensalt(rounds=4),
        ).decode("utf-8")

        async def fetchrow(query: str, station_id: str, username: str):
            assert "station_credentials" in query
            assert "username = $2 THEN 0" in query
            assert station_id == "pilot-depot-001"
            assert username == "TACW1000000G0001"
            return {"id": 2, "password_hash": alias_password_hash}

        conn = AsyncMock()
        conn.fetchrow.side_effect = fetchrow
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        supabase_client.db_pool = pool

        result = await supabase_client.validate_basic_auth(
            "pilot-depot-001",
            "TACW1000000G0001",
            "alias-password",
        )

        assert result is True
        conn.execute.assert_awaited_once_with(
            "UPDATE station_credentials SET last_used = NOW() WHERE id = $1",
            2,
        )

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_station_requires_basic_auth_reads_charging_stations(self, supabase_client):
        """Auth-required flag comes from Supabase charging station config."""
        supabase_client.fetch_one = AsyncMock(return_value={"auth_required": True})

        result = await supabase_client.station_requires_basic_auth("pilot-depot-001")

        assert result is True
        supabase_client.fetch_one.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_basic_auth_succeeds_when_last_used_update_fails(self, supabase_client):
        """A non-critical last_used update failure does not reject valid credentials."""
        password_hash = bcrypt.hashpw(
            b"valid-password",
            bcrypt.gensalt(rounds=4),
        ).decode("utf-8")
        supabase_client.fetch_one = AsyncMock(
            return_value={"id": 42, "password_hash": password_hash}
        )
        supabase_client.logger = MagicMock()

        conn = AsyncMock()
        conn.execute.side_effect = RuntimeError("pool exhausted")
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        supabase_client.db_pool = pool

        result = await supabase_client.validate_basic_auth(
            "pilot-depot-001",
            "TACW1000000G0001",
            "valid-password",
        )

        assert result is True
        conn.execute.assert_awaited_once_with(
            "UPDATE station_credentials SET last_used = NOW() WHERE id = $1",
            42,
        )
        supabase_client.logger.warning.assert_called_once()
