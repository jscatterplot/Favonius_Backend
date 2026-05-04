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
            return_value={"canonical_station_id": "hrx-uab_hrx-vilnius-001"}
        )

        result = await supabase_client.resolve_station_id("TACW1141622G1433")

        assert result == "hrx-uab_hrx-vilnius-001"
        supabase_client.fetch_one.assert_awaited_once()

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
            assert station_id == "hrx-uab_hrx-vilnius-001"
            assert username == "TACW1141622G1433"
            return {"id": 2, "password_hash": alias_password_hash}

        conn = AsyncMock()
        conn.fetchrow.side_effect = fetchrow
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        supabase_client.db_pool = pool

        result = await supabase_client.validate_basic_auth(
            "hrx-uab_hrx-vilnius-001",
            "TACW1141622G1433",
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

        result = await supabase_client.station_requires_basic_auth("hrx-uab_hrx-vilnius-001")

        assert result is True
        supabase_client.fetch_one.assert_awaited_once()
