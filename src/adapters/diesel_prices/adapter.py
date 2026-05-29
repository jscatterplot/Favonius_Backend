"""Diesel-price adapter: fetch from the source, store to ``diesel_prices``.

Mirrors :class:`~src.adapters.entsoe.prices.ENTSOEAdapter`:

- :meth:`get_current_prices` — one paginated source round → ``DieselPrice`` list,
  optionally filtered to a set of ISO country codes.
- :meth:`store_prices_to_db` — UNNEST bulk insert with
  ``ON CONFLICT (time, source, region) DO NOTHING RETURNING 1`` so concurrent
  ingestion (poller + read-through cache) can't race-insert duplicates, and the
  return value counts *actual* inserts.
- :meth:`get_prices_for_period` — read cached rows for a region/window.

Stored ``price_eur_per_l`` is the ex-tax (wholesale) figure; see ``mapping.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import asyncpg

from .client import DieselPriceClient
from .mapping import DieselPrice, parse_diesel_record

logger = logging.getLogger(__name__)


class DieselPriceAdapter:
    """Fetch + persist wholesale diesel prices."""

    def __init__(
        self,
        *,
        pool: Optional[asyncpg.Pool] = None,
        client: Optional[DieselPriceClient] = None,
    ) -> None:
        """Store the TimescaleDB pool and an (optionally injected) client."""
        self.pool = pool
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> DieselPriceClient:
        """Lazily build the source client (so tests can inject a fake)."""
        if self._client is None:
            self._client = DieselPriceClient()
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client (only when this adapter owns it)."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def get_current_prices(self, regions: Optional[list[str]] = None) -> list[DieselPrice]:
        """Fetch the latest per-country diesel prices from the source.

        ``regions`` (uppercase ISO alpha-2) filters the result; ``None`` returns
        everything the source publishes. Unparseable records are skipped (not
        fatal) so one bad row never aborts the cycle.
        """
        wanted = {r.upper() for r in regions} if regions else None
        source = self.client.source
        out: list[DieselPrice] = []
        async for raw in self.client.iter_prices():
            price = parse_diesel_record(raw, source=source)
            if price is None:
                continue
            if wanted is not None and price.region not in wanted:
                continue
            out.append(price)
        return out

    async def store_prices_to_db(self, prices: list[DieselPrice]) -> int:
        """Bulk-upsert prices; return the count of rows actually inserted."""
        if not self.pool:
            raise RuntimeError("Database pool not configured for DieselPriceAdapter")
        if not prices:
            return 0

        times = [p.time for p in prices]
        regions = [p.region for p in prices]
        sources = [p.source for p in prices]
        ex_tax = [p.price_eur_per_l for p in prices]
        inc_tax = [p.price_incl_tax_eur_per_l for p in prices]

        query = """
        INSERT INTO diesel_prices
            (time, region, source, price_eur_per_l, price_incl_tax_eur_per_l)
        SELECT * FROM UNNEST(
            $1::timestamptz[], $2::text[], $3::text[], $4::float8[], $5::float8[]
        )
        ON CONFLICT (time, source, region) DO NOTHING
        RETURNING 1
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    inserted = await conn.fetch(query, times, regions, sources, ex_tax, inc_tax)
            stored = len(inserted)
            logger.info("Stored %d/%d diesel prices", stored, len(prices))
            return stored
        except asyncpg.PostgresError as exc:
            logger.error("Database error storing diesel prices: %s", exc)
            raise

    async def get_prices_for_period(
        self,
        region: str,
        start: datetime,
        end: datetime,
        *,
        source: Optional[str] = None,
    ) -> list[DieselPrice]:
        """Read cached diesel prices for a region over [start, end]."""
        if not self.pool:
            raise RuntimeError("Database pool not configured for DieselPriceAdapter")
        src = source or self.client.source
        rows = await self.pool.fetch(
            """
            SELECT time, region, source, price_eur_per_l, price_incl_tax_eur_per_l
            FROM diesel_prices
            WHERE region = $1 AND source = $2 AND time >= $3 AND time <= $4
            ORDER BY time
            """,
            region.upper(),
            src,
            start,
            end,
        )
        return [
            DieselPrice(
                time=r["time"],
                region=r["region"],
                price_eur_per_l=float(r["price_eur_per_l"]),
                source=r["source"],
                price_incl_tax_eur_per_l=(
                    float(r["price_incl_tax_eur_per_l"])
                    if r["price_incl_tax_eur_per_l"] is not None
                    else None
                ),
            )
            for r in rows
        ]
