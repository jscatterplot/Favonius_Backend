"""Unit tests for the ENTSO-E branch of PriceIngestionService.

Covers two regressions:

  * Resolved bidding zone must be passed to ``get_prices_for_depot``.
    Earlier draft passed only ``depot_timezone``, so the adapter
    re-derived the zone via ``get_bidding_zone(tz)`` and ignored
    ``tariff_config['entsoe_zone']`` overrides — splitting the read
    path (Supabase override honoured) from the write path (timezone
    only). Storage and lookup must agree on the same zone.
  * Multi-depot ingestion in a shared zone must hit the ENTSO-E API
    only once per zone per run. The first depot pulls fresh data
    with ``use_cache=False``; subsequent depots in the same zone
    read from ``electricity_prices`` via ``use_cache=True``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.adapters.caiso.ingestion import PriceIngestionService


def _service_with_mocked_adapters():
    pool = MagicMock()
    caiso = MagicMock()
    entsoe = MagicMock()
    entsoe.get_prices_for_depot = AsyncMock(return_value=[
        # one truthy element so the storage path runs
        MagicMock(price_eur_mwh=42.0),
    ])
    entsoe.store_prices_to_db = AsyncMock(return_value=1)
    return PriceIngestionService(pool=pool, adapter=caiso, entsoe_adapter=entsoe)


@pytest.mark.asyncio
async def test_fetch_entsoe_prices_passes_resolved_zone_to_adapter():
    """The resolved zone must reach the adapter via ``bidding_zone``
    so the adapter doesn't fall back to timezone-derived lookup,
    which would skip ``tariff_config['entsoe_zone']`` overrides."""
    service = _service_with_mocked_adapters()
    depot_id = str(uuid4())
    override_zone = "10YDK-1--------W"  # DK1 — Denmark has two zones; timezone alone is ambiguous

    with patch(
        "src.db.queries.resolve_bidding_zone",
        new=AsyncMock(return_value=override_zone),
    ):
        await service._fetch_entsoe_prices(
            depot_id,
            datetime(2026, 5, 20, 10, 0),
            datetime(2026, 5, 22, 10, 0),
            depot_timezone="Europe/Copenhagen",
        )

    service.entsoe_adapter.get_prices_for_depot.assert_awaited_once()
    kwargs = service.entsoe_adapter.get_prices_for_depot.await_args.kwargs
    assert kwargs["bidding_zone"] == override_zone
    # The adapter handles storage internally now; ingestion no longer
    # double-writes. Read and write paths agree because both reuse the
    # same resolved zone the ingestion service injected.
    service.entsoe_adapter.store_prices_to_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_entsoe_prices_dedups_within_fetched_zones_set():
    """Second depot in the same zone must short-circuit before any adapter
    call. Earlier draft used ``use_cache=True`` for depots 2..N, but
    ``_get_cached_prices`` accepts any non-empty slice — including stale
    rows from a prior day's ingestion — and the second depot would
    silently inherit incomplete coverage. The first depot's fresh fetch
    is the canonical source for the run; the table is already populated
    for every consumer (calculator + optimizer) when the second depot's
    turn comes around."""
    service = _service_with_mocked_adapters()
    zone = "10YLT-1001A0008Q"  # Lithuania
    fetched_zones: set[str] = set()

    with patch(
        "src.db.queries.resolve_bidding_zone",
        new=AsyncMock(return_value=zone),
    ):
        # First depot — fetches and stores.
        await service._fetch_entsoe_prices(
            str(uuid4()),
            datetime(2026, 5, 20, 10, 0),
            datetime(2026, 5, 22, 10, 0),
            depot_timezone="Europe/Vilnius",
            fetched_zones=fetched_zones,
        )
        first_call = service.entsoe_adapter.get_prices_for_depot.await_args.kwargs
        assert first_call["use_cache"] is False
        assert zone in fetched_zones
        assert service.entsoe_adapter.get_prices_for_depot.await_count == 1

        # Second depot in the same zone — no adapter call at all.
        await service._fetch_entsoe_prices(
            str(uuid4()),
            datetime(2026, 5, 20, 10, 0),
            datetime(2026, 5, 22, 10, 0),
            depot_timezone="Europe/Vilnius",
            fetched_zones=fetched_zones,
        )
        assert service.entsoe_adapter.get_prices_for_depot.await_count == 1, (
            "second depot must not call the adapter — the table is already populated"
        )


@pytest.mark.asyncio
async def test_fetch_entsoe_prices_does_not_add_to_set_when_api_returns_nothing():
    """If the adapter returns no rows for the first depot, ``fetched_zones``
    must NOT record the zone — otherwise the second depot would skip
    its own API call and the run would silently produce zero prices
    for the entire zone."""
    pool = MagicMock()
    caiso = MagicMock()
    entsoe = MagicMock()
    entsoe.get_prices_for_depot = AsyncMock(return_value=[])  # nothing returned
    entsoe.store_prices_to_db = AsyncMock(return_value=0)
    service = PriceIngestionService(pool=pool, adapter=caiso, entsoe_adapter=entsoe)

    fetched_zones: set[str] = set()
    zone = "10YLT-1001A0008Q"
    with patch(
        "src.db.queries.resolve_bidding_zone",
        new=AsyncMock(return_value=zone),
    ):
        await service._fetch_entsoe_prices(
            str(uuid4()),
            datetime(2026, 5, 20, 10, 0),
            datetime(2026, 5, 22, 10, 0),
            depot_timezone="Europe/Vilnius",
            fetched_zones=fetched_zones,
        )
    assert zone not in fetched_zones


@pytest.mark.asyncio
async def test_fetch_prices_for_all_depots_shares_one_set_per_run():
    """``fetch_prices_for_all_depots`` should pass a single ``fetched_zones``
    set across the entire loop so two Lithuanian depots only fire one
    ENTSO-E request."""
    service = _service_with_mocked_adapters()

    rows = [
        {"depot_id": uuid4(), "utility_id": None, "timezone": "Europe/Vilnius"},
        {"depot_id": uuid4(), "utility_id": None, "timezone": "Europe/Vilnius"},
    ]
    # async with self.pool.acquire() as conn …
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    service.pool.acquire.return_value.__aenter__.return_value = conn
    service.pool.acquire.return_value.__aexit__.return_value = None

    zone = "10YLT-1001A0008Q"
    with patch(
        "src.db.queries.resolve_bidding_zone",
        new=AsyncMock(return_value=zone),
    ):
        await service.fetch_prices_for_all_depots()

    # Exactly one adapter call across both depots — the first one
    # fetches and stores; the second is short-circuited because the
    # zone is already in ``fetched_zones`` and ``electricity_prices``
    # already holds the data the second depot would have read.
    calls = service.entsoe_adapter.get_prices_for_depot.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["use_cache"] is False
