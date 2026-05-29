"""Diesel wholesale-price poller.

Background loop that fetches the latest per-country wholesale diesel prices and
upserts them into the ``diesel_prices`` hypertable, where
``src/db/queries.py::fetch_or_pull_diesel_price`` reads them for the
EV-vs-diesel cost-per-km report.

Structurally identical to ``src/adapters/navirec/poller.py``: env-gated
(``DIESEL_PRICE_POLL_ENABLED``, default off), never raises out of the loop, one
source round per cycle. Because the bulletin updates weekly, a generous default
interval (1h) keeps the cache warm without hammering the source.

One cycle:
  1. Enumerate the distinct countries the platform's depots are in (resolved
     from each depot's timezone, with a ``tariff_config['diesel_country']``
     override) so we only fetch regions we actually bill.
  2. Fetch the latest prices, filtered to those regions.
  3. Upsert (idempotent). Per-region write counts feed a metric.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from ...monitoring.metrics import (
    DIESEL_POLL_CYCLES,
    DIESEL_POLL_DURATION,
    DIESEL_PRICE_FETCH_FAILURES,
    DIESEL_PRICES_WRITTEN,
)
from .adapter import DieselPriceAdapter
from .client import DieselPriceClient, DieselPriceClientError

logger = logging.getLogger(__name__)


def _default_interval_s() -> float:
    """Resolve the poll interval (seconds), floored at 5 minutes."""
    try:
        return max(300.0, float(os.getenv("DIESEL_PRICE_POLL_INTERVAL_S", "3600")))
    except ValueError:
        return 3600.0


def _poll_enabled() -> bool:
    return os.getenv("DIESEL_PRICE_POLL_ENABLED", "false").strip().lower() == "true"


async def _depot_regions(static_pool: Any) -> list[str]:
    """Return the distinct ISO country codes the platform's depots are in.

    Resolved per depot via ``tariff_config['diesel_country']`` override →
    timezone → country map. Depots whose country can't be resolved are skipped
    (we can't price a region we can't name).
    """
    from ...db.queries import resolve_country_code  # local import: avoid cycle

    async with static_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM sites")
        regions: set[str] = set()
        for row in rows:
            code = await resolve_country_code(conn, row["id"])
            if code:
                regions.add(code.upper())
    return sorted(regions)


async def poll_once(
    static_pool: Any,
    ts_pool: Any,
    adapter: DieselPriceAdapter,
) -> dict[str, Any]:
    """Run one poll cycle. Returns a summary dict (used by tests + logging)."""
    regions = await _depot_regions(static_pool)
    if not regions:
        logger.debug("Diesel poll: no resolvable depot regions; nothing to fetch")
        return {"regions": [], "written": 0}

    prices = await adapter.get_current_prices(regions)
    written = await adapter.store_prices_to_db(prices)

    # Per-region write attribution (best-effort; the upsert counts true inserts
    # in aggregate, so attribute the fetched set's regions for the metric).
    per_region: dict[str, int] = {}
    for price in prices:
        per_region[price.region] = per_region.get(price.region, 0) + 1
    for region, count in per_region.items():
        DIESEL_PRICES_WRITTEN.labels(region=region, source=adapter.client.source).inc(count)

    return {"regions": regions, "fetched": len(prices), "written": written}


async def run_diesel_poll_loop(
    static_pool: Any,
    ts_pool: Any,
    *,
    interval_s: Optional[float] = None,
    client: Optional[DieselPriceClient] = None,
) -> None:
    """Background loop: fetch diesel prices every ``interval_s``. Gated by env flag.

    Never raises out to the caller — a failed cycle is logged and the loop
    continues. Disabled (returns immediately) unless
    ``DIESEL_PRICE_POLL_ENABLED=true``.
    """
    if not _poll_enabled():
        DIESEL_POLL_CYCLES.labels(outcome="skipped_disabled").inc()
        logger.info("Diesel price poller disabled (set DIESEL_PRICE_POLL_ENABLED=true to enable)")
        return

    interval = interval_s if interval_s is not None else _default_interval_s()
    owns_client = client is None
    try:
        client = client or DieselPriceClient()
    except DieselPriceClientError as exc:
        logger.warning("Diesel price poller not started: %s", exc)
        return

    adapter = DieselPriceAdapter(pool=ts_pool, client=client)
    logger.info("Diesel price poller started (source=%s interval=%.0fs)", client.source, interval)
    try:
        while True:
            start = asyncio.get_event_loop().time()
            try:
                summary = await poll_once(static_pool, ts_pool, adapter)
                DIESEL_POLL_CYCLES.labels(outcome="ok").inc()
                logger.debug("Diesel poll cycle: %s", summary)
            except Exception:  # noqa: BLE001 — keep the loop alive across cycles
                logger.exception("Diesel price poll cycle failed")
                DIESEL_POLL_CYCLES.labels(outcome="fetch_error").inc()
                DIESEL_PRICE_FETCH_FAILURES.labels(source=client.source, reason="cycle_error").inc()
            finally:
                DIESEL_POLL_DURATION.observe(asyncio.get_event_loop().time() - start)
            await asyncio.sleep(interval)
    finally:
        if owns_client:
            await adapter.aclose()
