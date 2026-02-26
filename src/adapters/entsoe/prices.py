"""ENTSO-E Transparency Platform price feed adapter.

Fetches day-ahead electricity prices from the ENTSO-E Transparency Platform
REST API for European bidding zones.

Reference: https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import asyncpg
import httpx

from .mappings import get_bidding_zone

logger = logging.getLogger(__name__)

# ENTSO-E API XML namespace
_NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"

# Document type for day-ahead prices
_DOC_TYPE_DAY_AHEAD = "A44"


@dataclass
class ENTSOEPrice:
    """ENTSO-E electricity price data point.

    Attributes:
        timestamp: Price timestamp (UTC)
        price_eur_mwh: Day-ahead price in EUR/MWh
        bidding_zone: EIC area code of the bidding zone
        currency: Currency code (typically EUR)
        resolution: Time resolution (e.g., 'PT60M', 'PT15M')
    """

    timestamp: datetime
    price_eur_mwh: float
    bidding_zone: str
    currency: str = "EUR"
    resolution: str = "PT60M"

    @property
    def price_per_kwh(self) -> float:
        """Price converted to EUR/kWh (or local currency/kWh)."""
        return self.price_eur_mwh / 1000.0


class ENTSOEAdapter:
    """Adapter for ENTSO-E Transparency Platform day-ahead price data.

    Fetches hourly (or 15-min) day-ahead electricity prices for European
    bidding zones via the ENTSO-E REST API.

    The API returns XML documents (Publication_MarketDocument) containing
    TimeSeries with hourly price points.
    """

    BASE_URL = "https://web-api.tp.entsoe.eu/api"

    def __init__(
        self,
        security_token: Optional[str] = None,
        pool: Optional[asyncpg.Pool] = None,
    ):
        """Initialize ENTSO-E adapter.

        Args:
            security_token: ENTSO-E API security token. Falls back to
                EUROPEAN_ELECTRICITY_API environment variable.
            pool: Optional database connection pool for price storage
        """
        self.security_token = security_token or os.getenv("EUROPEAN_ELECTRICITY_API", "")
        if not self.security_token:
            logger.warning(
                "No ENTSO-E security token provided. Set EUROPEAN_ELECTRICITY_API "
                "environment variable or pass security_token parameter."
            )
        self.pool = pool
        self.client = httpx.AsyncClient(timeout=30.0)
        logger.info("Initialized ENTSOEAdapter")

    async def get_day_ahead_prices(
        self,
        start_date: datetime,
        end_date: datetime,
        bidding_zone: Optional[str] = None,
        depot_timezone: Optional[str] = None,
    ) -> list[ENTSOEPrice]:
        """Fetch day-ahead prices from ENTSO-E Transparency Platform.

        Args:
            start_date: Start of price window (UTC)
            end_date: End of price window (UTC)
            bidding_zone: EIC area code. If None, derived from depot_timezone.
            depot_timezone: IANA timezone to derive bidding zone from.

        Returns:
            List of ENTSOEPrice objects for each hour in the window

        Raises:
            ValueError: If no bidding zone can be determined
            httpx.HTTPStatusError: On API errors
        """
        zone = bidding_zone or (get_bidding_zone(depot_timezone) if depot_timezone else None)
        if not zone:
            raise ValueError(
                f"Cannot determine bidding zone. Provide bidding_zone or a "
                f"European depot_timezone (got timezone={depot_timezone!r})"
            )

        if not self.security_token:
            raise ValueError("ENTSO-E security token not configured")

        params = {
            "securityToken": self.security_token,
            "documentType": _DOC_TYPE_DAY_AHEAD,
            "in_Domain": zone,
            "out_Domain": zone,
            "periodStart": _format_entsoe_time(start_date),
            "periodEnd": _format_entsoe_time(end_date),
        }

        logger.debug(
            "Fetching ENTSO-E prices for zone %s from %s to %s",
            zone,
            start_date,
            end_date,
        )

        response = await self.client.get(self.BASE_URL, params=params)

        if response.status_code == 429:
            logger.warning("ENTSO-E rate limit hit (429). Back off before retrying.")
            raise RuntimeError("ENTSO-E API rate limit exceeded (429)")

        if response.status_code != 200:
            logger.error(
                "ENTSO-E API error: HTTP %d - %s",
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError(f"ENTSO-E API returned HTTP {response.status_code}")

        prices = _parse_price_document(response.text, zone)
        logger.info(
            "Fetched %d price points for zone %s (%s to %s)",
            len(prices),
            zone,
            start_date,
            end_date,
        )
        return prices

    async def get_current_price(
        self,
        bidding_zone: Optional[str] = None,
        depot_timezone: Optional[str] = None,
    ) -> Optional[ENTSOEPrice]:
        """Get current hour's price.

        Args:
            bidding_zone: EIC area code
            depot_timezone: IANA timezone to derive bidding zone

        Returns:
            ENTSOEPrice for current hour, or None if unavailable
        """
        now = datetime.now(timezone.utc)
        try:
            prices = await self.get_day_ahead_prices(
                now,
                now + timedelta(hours=1),
                bidding_zone=bidding_zone,
                depot_timezone=depot_timezone,
            )
            return prices[0] if prices else None
        except Exception as exc:
            logger.error("Failed to get current ENTSO-E price: %s", exc)
            return None

    async def store_prices_to_db(
        self,
        prices: list[ENTSOEPrice],
        depot_id: str | UUID,
        source: str = "entsoe_dam",
        demand_charge_per_kw: Optional[float] = None,
    ) -> int:
        """Store fetched prices to database.

        Converts EUR/MWh to EUR/kWh and stores in the prices table
        using the same schema as CAISO prices.

        Args:
            prices: List of ENTSOEPrice objects
            depot_id: Depot identifier
            source: Price source identifier
            demand_charge_per_kw: Optional demand charge rate

        Returns:
            Number of prices stored
        """
        if not self.pool:
            raise RuntimeError("Database pool not configured for ENTSOEAdapter")

        if not prices:
            return 0

        depot_id_str = str(depot_id)
        query = """
        INSERT INTO prices (time, depot_id, energy_kwh, demand_kw, source)
        VALUES ($1, $2::uuid, $3, $4, $5)
        ON CONFLICT (time, depot_id) DO UPDATE
        SET energy_kwh = EXCLUDED.energy_kwh,
            demand_kw = EXCLUDED.demand_kw,
            source = EXCLUDED.source
        """

        stored_count = 0
        try:
            async with self.pool.acquire() as conn:
                for price in prices:
                    energy_kwh = price.price_per_kwh
                    await conn.execute(
                        query,
                        price.timestamp,
                        depot_id_str,
                        energy_kwh,
                        demand_charge_per_kw,
                        source,
                    )
                    stored_count += 1

            logger.info(
                "Stored %d ENTSO-E prices for depot %s (source: %s)",
                stored_count,
                depot_id_str,
                source,
            )
            return stored_count

        except asyncpg.PostgresError as e:
            logger.error("Database error storing ENTSO-E prices: %s", e)
            raise

    async def get_prices_for_depot(
        self,
        depot_id: str | UUID,
        start_date: datetime,
        end_date: datetime,
        depot_timezone: Optional[str] = None,
        bidding_zone: Optional[str] = None,
        use_cache: bool = True,
        source: str = "entsoe_dam",
    ) -> list[ENTSOEPrice]:
        """Get prices for a specific depot, with caching support.

        Tries cached prices from database first. If unavailable,
        fetches from ENTSO-E API and stores them.

        Args:
            depot_id: Depot identifier
            start_date: Start of price window
            end_date: End of price window
            depot_timezone: IANA timezone for bidding zone resolution
            bidding_zone: Direct EIC area code (overrides timezone lookup)
            use_cache: Whether to try cached prices first
            source: Price source for storage

        Returns:
            List of ENTSOEPrice objects
        """
        zone = bidding_zone or (get_bidding_zone(depot_timezone) if depot_timezone else None)

        if use_cache and self.pool:
            try:
                cached = await self._get_cached_prices(
                    depot_id, start_date, end_date, zone or "unknown"
                )
                if cached:
                    return cached
            except Exception as e:
                logger.warning("Error getting cached ENTSO-E prices: %s", e)

        prices = await self.get_day_ahead_prices(
            start_date,
            end_date,
            bidding_zone=zone,
            depot_timezone=depot_timezone,
        )

        if self.pool and prices:
            try:
                await self.store_prices_to_db(prices, depot_id, source=source)
            except Exception as e:
                logger.warning("Error storing ENTSO-E prices to database: %s", e)

        return prices

    async def _get_cached_prices(
        self,
        depot_id: str | UUID,
        start_time: datetime,
        end_time: datetime,
        bidding_zone: str,
    ) -> list[ENTSOEPrice]:
        """Get cached prices from database."""
        if not self.pool:
            return []

        depot_id_str = str(depot_id)
        query = """
        SELECT time, energy_kwh, source
        FROM prices
        WHERE depot_id = $1::uuid
          AND time >= $2
          AND time < $3
          AND source = 'entsoe_dam'
        ORDER BY time
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, depot_id_str, start_time, end_time)

        if not rows:
            return []

        prices = []
        for row in rows:
            price_mwh = row["energy_kwh"] * 1000.0
            prices.append(
                ENTSOEPrice(
                    timestamp=row["time"],
                    price_eur_mwh=price_mwh,
                    bidding_zone=bidding_zone,
                )
            )

        logger.debug(
            "Using %d cached ENTSO-E prices for depot %s",
            len(prices),
            depot_id_str,
        )
        return prices

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()


def _format_entsoe_time(dt: datetime) -> str:
    """Format datetime for ENTSO-E API (YYYYMMddHHmm in UTC).

    Args:
        dt: Datetime to format (converted to UTC if timezone-aware)

    Returns:
        String in YYYYMMddHHmm format
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d%H%M")


def _parse_price_document(xml_text: str, bidding_zone: str) -> list[ENTSOEPrice]:
    """Parse ENTSO-E Publication_MarketDocument XML into ENTSOEPrice list.

    Handles:
    - Multiple TimeSeries elements
    - Multiple Period elements (DST transitions)
    - Both PT60M (hourly) and PT15M (quarter-hourly) resolutions
    - Curve type A03 (step curve) with skipped positions

    Args:
        xml_text: Raw XML response from ENTSO-E API
        bidding_zone: EIC area code for attribution

    Returns:
        List of ENTSOEPrice objects sorted by timestamp
    """
    root = ET.fromstring(xml_text)
    prices: list[ENTSOEPrice] = []

    for ts in root.iter(f"{{{_NS}}}TimeSeries"):
        currency_el = ts.find(f"{{{_NS}}}currency_Unit.name")
        currency = currency_el.text if currency_el is not None else "EUR"

        for period in ts.iter(f"{{{_NS}}}Period"):
            # Parse time interval
            interval = period.find(f"{{{_NS}}}timeInterval")
            if interval is None:
                continue

            start_el = interval.find(f"{{{_NS}}}start")
            if start_el is None or start_el.text is None:
                continue
            period_start = _parse_utc_time(start_el.text)

            # Parse resolution
            res_el = period.find(f"{{{_NS}}}resolution")
            resolution = res_el.text if res_el is not None else "PT60M"
            step_minutes = 15 if resolution == "PT15M" else 60

            # Parse price points
            point_map: dict[int, float] = {}
            for point in period.iter(f"{{{_NS}}}Point"):
                pos_el = point.find(f"{{{_NS}}}position")
                price_el = point.find(f"{{{_NS}}}price.amount")
                if pos_el is not None and price_el is not None:
                    try:
                        position = int(pos_el.text)
                        price_amount = float(price_el.text)
                        point_map[position] = price_amount
                    except (ValueError, TypeError):
                        continue

            if not point_map:
                continue

            # Build continuous price series (handle A03 step curves with gaps)
            max_pos = max(point_map.keys())
            last_price = 0.0

            for pos in range(1, max_pos + 1):
                if pos in point_map:
                    last_price = point_map[pos]

                ts_time = period_start + timedelta(minutes=(pos - 1) * step_minutes)
                prices.append(
                    ENTSOEPrice(
                        timestamp=ts_time,
                        price_eur_mwh=last_price,
                        bidding_zone=bidding_zone,
                        currency=currency,
                        resolution=resolution,
                    )
                )

    # Sort by timestamp and deduplicate
    prices.sort(key=lambda p: p.timestamp)
    seen: set[datetime] = set()
    unique_prices: list[ENTSOEPrice] = []
    for p in prices:
        if p.timestamp not in seen:
            seen.add(p.timestamp)
            unique_prices.append(p)

    return unique_prices


def _parse_utc_time(time_str: str) -> datetime:
    """Parse ENTSO-E UTC time string (e.g., '2026-02-09T23:00Z').

    Args:
        time_str: Time string from ENTSO-E XML

    Returns:
        Timezone-aware datetime in UTC
    """
    time_str = time_str.strip()
    if time_str.endswith("Z"):
        time_str = time_str[:-1] + "+00:00"
    return datetime.fromisoformat(time_str)
