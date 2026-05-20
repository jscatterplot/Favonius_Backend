"""Unit tests for the backfill script's SiteResolver helper.

Regression: ``insert_open_session`` writes the OCPP station_id but not
``site_id`` (no Supabase round-trip on the StartTransaction hot path).
The WS-handler's post-close cost task backfills ``site_id`` in memory,
but if that task is killed before write (process restart, crashed
event loop, etc.) the row sits at ``site_id=NULL`` and the documented
backfill safety net is supposed to recover it. Without ``SiteResolver``
the backfill's existing ``ZoneResolver`` short-circuits on NULL
``site_id`` and the row never gets priced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from scripts.backfill_session_cost import SiteResolver


def _pool_returning(rows):
    """Construct a MagicMock asyncpg-like pool whose fetchrow walks ``rows``.

    Pass a list when the caller will issue multiple lookups so each
    receives the corresponding row; pass a single dict / None for a
    one-shot lookup.
    """
    conn = AsyncMock()
    if isinstance(rows, list):
        conn.fetchrow = AsyncMock(side_effect=rows)
    else:
        conn.fetchrow = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.mark.asyncio
async def test_site_resolver_returns_resolved_site_id():
    expected = uuid4()
    pool, conn = _pool_returning({"site_id": expected})
    resolver = SiteResolver(pool)

    assert await resolver("CP-001") == expected
    conn.fetchrow.assert_awaited_once()
    assert "CP-001" in conn.fetchrow.await_args.args


@pytest.mark.asyncio
async def test_site_resolver_caches_repeated_station_lookups():
    """Thousands of sessions on the same charger should produce one
    Supabase round-trip, not one per row."""
    expected = uuid4()
    pool, conn = _pool_returning({"site_id": expected})
    resolver = SiteResolver(pool)

    for _ in range(50):
        assert await resolver("CP-001") == expected

    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_site_resolver_caches_negative_results():
    """An unknown station shouldn't keep re-querying Supabase. The
    backfill may hit the same orphan station_id across many rows;
    caching None avoids amplifying every backfill into a flood of
    pointless lookups."""
    pool, conn = _pool_returning(None)
    resolver = SiteResolver(pool)

    for _ in range(10):
        assert await resolver("UNKNOWN-CP") is None

    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_site_resolver_handles_none_or_empty_station_id():
    """NULL / empty station_id is a no-op — both are sentinels for
    'this row has no charger info' (terra import rows, malformed
    fixtures). Don't burn a DB round-trip on them."""
    pool, conn = _pool_returning(None)
    resolver = SiteResolver(pool)

    assert await resolver(None) is None
    assert await resolver("") is None
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_site_resolver_distinct_stations_each_resolve_once():
    site_a = uuid4()
    site_b = uuid4()
    pool, conn = _pool_returning([
        {"site_id": site_a},
        {"site_id": site_b},
    ])
    resolver = SiteResolver(pool)

    assert await resolver("CP-A") == site_a
    assert await resolver("CP-B") == site_b
    # Repeats hit the cache, not the DB.
    assert await resolver("CP-A") == site_a
    assert await resolver("CP-B") == site_b
    assert conn.fetchrow.await_count == 2
