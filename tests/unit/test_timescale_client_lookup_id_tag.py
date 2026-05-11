"""Unit tests for TimescaleClient.lookup_id_tag resilience.

Covers the cards-only authorization path and tolerance to missing
reference tables (``vehicles``, ``drivers``, assignment join tables) so a
cards-only fleet — or a deployment mid-migration — can still authorize
RFID tags.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.timescale_client import TimescaleClient


def _make_client_with_conn(conn):
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost",
        user="user",
        password="pass",
    )
    client = TimescaleClient(config)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    client.pg_pool = pool
    return client


@pytest.fixture(autouse=True)
def _reset_missing_table_log_cache():
    """Make ``_log_missing_reference_table_once`` fire every test.

    The cache is process-wide so without this fixture a test that runs
    after another that already logged ``vehicles`` would silently no-op.
    """
    TimescaleClient._missing_reference_table_logs.clear()
    yield
    TimescaleClient._missing_reference_table_logs.clear()


def _vehicles_undefined_table_error() -> asyncpg.exceptions.UndefinedTableError:
    return asyncpg.exceptions.UndefinedTableError('relation "vehicles" does not exist')


@pytest.mark.asyncio
async def test_falls_through_to_cards_when_vehicles_table_missing():
    """`vehicles` missing must not block an active card from authorizing."""
    conn = AsyncMock()
    # 1st fetch (vehicles) raises; 2nd fetch (rfid_cards) succeeds.
    conn.fetch = AsyncMock(
        side_effect=[
            _vehicles_undefined_table_error(),
            [{"card_id": "card-uuid-1", "depot_id": "depot-uuid-1"}],
        ]
    )
    # Enrichment queries return None (no assignment).
    conn.fetchval = AsyncMock(return_value=None)

    client = _make_client_with_conn(conn)

    result = await client.lookup_id_tag("CARD-TAG", station_id="STATION-1")

    assert result == {
        "card_id": "card-uuid-1",
        "depot_id": "depot-uuid-1",
        "vehicle_id": None,
        "driver_id": None,
        "source": "rfid_card",
    }


@pytest.mark.asyncio
async def test_card_authorizes_with_no_vehicle_or_driver_attached():
    """A card row alone is sufficient — no assignments required."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # vehicles: no match
            [{"card_id": "card-2", "depot_id": "depot-2"}],
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)

    client = _make_client_with_conn(conn)

    result = await client.lookup_id_tag("LOOSE-CARD")

    assert result["card_id"] == "card-2"
    assert result["vehicle_id"] is None
    assert result["driver_id"] is None
    assert result["source"] == "rfid_card"


@pytest.mark.asyncio
async def test_returns_none_when_rfid_cards_table_missing():
    """Cards table missing → return None, caller decides INVALID."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # vehicles: no match
            asyncpg.exceptions.UndefinedTableError('relation "rfid_cards" does not exist'),
        ]
    )

    client = _make_client_with_conn(conn)

    result = await client.lookup_id_tag("ANY-TAG")
    assert result is None


@pytest.mark.asyncio
async def test_assignment_table_missing_does_not_block_card_authorization():
    """Cards still authorize when the join tables are absent."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # vehicles
            [{"card_id": "card-3", "depot_id": "depot-3"}],
        ]
    )
    # vehicle assignment lookup raises; driver assignment returns None.
    conn.fetchval = AsyncMock(
        side_effect=[
            asyncpg.exceptions.UndefinedTableError(
                'relation "rfid_card_vehicle_assignments" does not exist'
            ),
            None,
        ]
    )

    client = _make_client_with_conn(conn)

    result = await client.lookup_id_tag("ANY-TAG")
    assert result is not None
    assert result["card_id"] == "card-3"
    assert result["vehicle_id"] is None
    assert result["driver_id"] is None


@pytest.mark.asyncio
async def test_vehicle_primary_lookup_short_circuits_when_table_present():
    """Vehicle-primary tag still wins when ``vehicles`` exists and matches.

    Regression guard: the resilience refactor must not turn the
    vehicle-primary path into a fall-through.
    """
    conn = AsyncMock()
    vehicle_row = {
        "vehicle_id": "veh-1",
        "depot_id": "depot-1",
        "driver_id": None,
        "card_id": None,
        "source": "vehicle",
    }
    conn.fetch = AsyncMock(return_value=[vehicle_row])

    client = _make_client_with_conn(conn)

    result = await client.lookup_id_tag("VEH-TAG")

    assert result == vehicle_row
    # Cards lookup must NOT have run.
    assert conn.fetch.await_count == 1


@pytest.mark.asyncio
async def test_generic_db_error_still_propagates():
    """Only ``UndefinedTableError`` is swallowed; transient DB errors must surface.

    The caller in ``rfid_authorization.authorize`` already handles a
    generic exception (logs and returns INVALID). Catching too broadly
    here would mask outages.
    """
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("connection reset"))

    client = _make_client_with_conn(conn)

    with pytest.raises(RuntimeError, match="connection reset"):
        await client.lookup_id_tag("ANY-TAG")


@pytest.mark.asyncio
async def test_missing_table_logged_once_per_process():
    """Schema drift surfaces in logs but does not flood on every Authorize."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            _vehicles_undefined_table_error(),
            [],
            _vehicles_undefined_table_error(),
            [],
        ]
    )

    client = _make_client_with_conn(conn)
    client.logger = MagicMock()

    await client.lookup_id_tag("TAG-A")
    await client.lookup_id_tag("TAG-B")

    # Two Authorize attempts but only one warning per missing table.
    assert client.logger.warning.call_count == 1


@pytest.mark.asyncio
async def test_lookup_id_tag_routes_to_supabase_pool_when_wired_in():
    """Static identity tables live in Supabase; the lookup must hit that pool.

    Regression: the lookup previously ran against ``pg_pool`` (TimescaleDB),
    where migration 029 dropped ``vehicles`` / ``charging_stations`` and the
    legacy ``rfid_cards`` shadow has the wrong column names. The result was
    ``column c.id does not exist`` and a hard Invalid for every card.
    """
    supabase_conn = AsyncMock()
    supabase_conn.fetch = AsyncMock(
        side_effect=[
            [],  # vehicles: no match
            [{"card_id": "card-supabase", "depot_id": "depot-1"}],
        ]
    )
    supabase_conn.fetchval = AsyncMock(return_value=None)
    supabase_pool = MagicMock()
    supabase_pool.acquire.return_value.__aenter__.return_value = supabase_conn
    supabase_pool.acquire.return_value.__aexit__.return_value = None

    # The TimescaleDB pool must NOT be touched once Supabase is wired in.
    timescale_conn = AsyncMock()
    timescale_pool = MagicMock()
    timescale_pool.acquire.return_value.__aenter__.return_value = timescale_conn
    timescale_pool.acquire.return_value.__aexit__.return_value = None

    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost",
        user="user",
        password="pass",
    )
    client = TimescaleClient(config)
    client.pg_pool = timescale_pool
    supabase_client = MagicMock()
    supabase_client.db_pool = supabase_pool
    client.set_supabase_client(supabase_client)

    result = await client.lookup_id_tag("CARD-TAG", station_id="STATION-1")

    assert result is not None
    assert result["card_id"] == "card-supabase"
    assert supabase_conn.fetch.await_count == 2
    timescale_conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_id_tag_falls_back_to_pg_pool_when_supabase_not_wired():
    """Without ``set_supabase_client`` the lookup uses ``pg_pool`` (legacy)."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],
            [{"card_id": "card-legacy", "depot_id": "depot-1"}],
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)

    client = _make_client_with_conn(conn)
    # No set_supabase_client call → _static_pool() must return pg_pool.

    result = await client.lookup_id_tag("CARD-TAG", station_id="STATION-1")

    assert result is not None
    assert result["card_id"] == "card-legacy"


@pytest.mark.asyncio
async def test_lookup_id_tag_falls_back_when_supabase_db_pool_is_none():
    """A SupabaseClient that hasn't connected yet must not break the lookup."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],
            [{"card_id": "card-fallback", "depot_id": "depot-1"}],
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)

    client = _make_client_with_conn(conn)
    half_wired = MagicMock()
    half_wired.db_pool = None
    client.set_supabase_client(half_wired)

    result = await client.lookup_id_tag("CARD-TAG", station_id="STATION-1")

    assert result is not None
    assert result["card_id"] == "card-fallback"


@pytest.mark.asyncio
async def test_multiple_active_cards_for_same_tag_rejected():
    """Duplicate-tag protection survives the refactor."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],
            [
                {"card_id": "card-a", "depot_id": "depot-1"},
                {"card_id": "card-b", "depot_id": "depot-1"},
            ],
        ]
    )

    client = _make_client_with_conn(conn)

    result = await client.lookup_id_tag("DUPLICATED-TAG")
    assert result is None
