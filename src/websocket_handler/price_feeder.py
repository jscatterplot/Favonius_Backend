"""ENTSO-E electricity price feeder service.

Fetches day-ahead prices from the ENTSO-E Transparency Platform and stores
them in TimescaleDB. CAISO support was removed once the project pivoted
fully to European deployments; see migration 034 for the canonical
``electricity_prices`` schema.
"""

import asyncio
import contextlib
import os
try:
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover - fallback when optional dependency is unavailable
    import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiohttp

from .config import PriceFeederConfig
from .monitoring import get_logger
from .timescale_client import TimescaleClient

# ENTSO-E constants
_ENTSOE_BASE_URL = "https://web-api.tp.entsoe.eu/api"
_ENTSOE_NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def _format_entsoe_time(dt: datetime) -> str:
    """Format datetime for ENTSO-E API (YYYYMMddHHmm in UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d%H%M")


class PriceFeederService:
    """Fetches day-ahead electricity prices from ENTSO-E and stores them in TimescaleDB.

    Each configured ENTSO-E bidding zone (EIC code) is polled on the
    ``fetch_interval_seconds`` cadence. Authentication uses the
    ``EUROPEAN_ELECTRICITY_API`` security token issued by the ENTSO-E
    Transparency Platform.
    """

    def __init__(
        self,
        config: PriceFeederConfig,
        timescale_client: TimescaleClient,
    ) -> None:
        self.config = config
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._optimization_engine = None
        self._entsoe_token = os.getenv("EUROPEAN_ELECTRICITY_API", "")

    def set_optimization_engine(self, engine) -> None:
        """Attach optimization engine to notify when prices change."""
        self._optimization_engine = engine

    async def start(self) -> None:
        """Start periodic price fetching."""
        if not self.config.enabled:
            self.logger.info("Price feeder disabled via configuration")
            return

        if self._running:
            return

        if not self.config.entsoe_zones:
            self.logger.warning(
                "Price feeder started with no PRICE_FEEDER_ENTSOE_ZONES configured; "
                "no prices will be fetched"
            )
        elif not self._entsoe_token:
            self.logger.warning(
                "Price feeder started with ENTSO-E zones configured but "
                "EUROPEAN_ELECTRICITY_API token unset; no prices will be fetched"
            )

        timeout = aiohttp.ClientTimeout(total=60)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info(
            "Price feeder started for ENTSO-E zones: %s with interval %ss",
            ", ".join(self.config.entsoe_zones) if self.config.entsoe_zones else "(none)",
            self.config.fetch_interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the price feeder."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.session:
            await self.session.close()
            self.session = None
        self.logger.info("Price feeder stopped")

    async def trigger_fetch(self) -> None:
        """Manually trigger a price refresh."""
        try:
            await self._fetch_and_store_prices()
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.error(f"Manual price fetch failed: {exc}")

    async def _run_loop(self) -> None:
        """Background loop for periodic fetching with exponential backoff."""
        await asyncio.sleep(5)  # small delay to allow startup
        consecutive_failures = 0
        max_backoff = 300  # 5 minutes max backoff

        while self._running:
            try:
                await self._fetch_and_store_prices()
                consecutive_failures = 0  # Reset on success
            except Exception as exc:
                consecutive_failures += 1
                # Exponential backoff: 2^failures seconds, capped at max_backoff
                backoff = min(2**consecutive_failures, max_backoff)
                self.logger.error(
                    f"Price feeder loop error (failure {consecutive_failures}): {exc}. "
                    f"Backing off {backoff}s"
                )
                await asyncio.sleep(backoff)
                continue  # Skip normal sleep, we already waited

            await asyncio.sleep(self.config.fetch_interval_seconds)

    async def _fetch_and_store_prices(self) -> None:
        """Fetch ENTSO-E day-ahead prices for all configured zones and store them."""
        if not self.session:
            return
        if not self.config.entsoe_zones or not self._entsoe_token:
            return

        end_time = datetime.now(timezone.utc) + timedelta(hours=self.config.lookahead_hours)
        start_time = datetime.now(timezone.utc) - timedelta(hours=1)

        all_points: List[dict] = []
        for zone_id in self.config.entsoe_zones:
            try:
                points = await self._fetch_entsoe_zone(zone_id, start_time, end_time)
                all_points.extend(points)
            except Exception as exc:
                self.logger.error(f"Failed to fetch ENTSO-E prices for zone {zone_id}: {exc}")

        if not all_points:
            self.logger.warning("No price data fetched in this interval")
            return

        await self.timescale_client.store_electricity_prices(all_points)
        self.logger.info("Stored %d electricity price points", len(all_points))

        if self._optimization_engine:
            await self._optimization_engine.request_run("price_update")

    async def _fetch_entsoe_zone(
        self, zone_id: str, start_time: datetime, end_time: datetime
    ) -> List[dict]:
        """Fetch day-ahead prices from ENTSO-E for a single bidding zone.

        Args:
            zone_id: EIC area code (e.g., '10YDE-RWENET---I')
            start_time: Start of query window (UTC)
            end_time: End of query window (UTC)

        Returns:
            List of price point dicts compatible with timescale_client.store_electricity_prices
        """
        if not self.session:
            return []

        params = {
            "securityToken": self._entsoe_token,
            "documentType": "A44",
            "in_Domain": zone_id,
            "out_Domain": zone_id,
            "periodStart": _format_entsoe_time(start_time),
            "periodEnd": _format_entsoe_time(end_time),
        }

        async with self.session.get(_ENTSOE_BASE_URL, params=params) as response:
            if response.status == 429:
                raise RuntimeError("ENTSO-E rate limit exceeded (429)")
            if response.status != 200:
                raise RuntimeError(f"ENTSO-E response code: {response.status}")
            body = await response.text()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._parse_entsoe_xml, body, zone_id)

    def _parse_entsoe_xml(self, xml_text: str, zone_id: str) -> List[dict]:
        """Parse ENTSO-E Publication_MarketDocument XML into price point dicts.

        Returns dicts compatible with timescale_client.store_electricity_prices.
        """
        points: List[dict] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            self.logger.error(f"Failed to parse ENTSO-E XML: {exc}")
            return points

        for ts in root.iter(f"{{{_ENTSOE_NS}}}TimeSeries"):
            for period in ts.iter(f"{{{_ENTSOE_NS}}}Period"):
                interval = period.find(f"{{{_ENTSOE_NS}}}timeInterval")
                if interval is None:
                    continue

                start_el = interval.find(f"{{{_ENTSOE_NS}}}start")
                if start_el is None or start_el.text is None:
                    continue

                time_str = start_el.text.strip()
                if time_str.endswith("Z"):
                    time_str = time_str[:-1] + "+00:00"
                period_start = datetime.fromisoformat(time_str)

                res_el = period.find(f"{{{_ENTSOE_NS}}}resolution")
                resolution = res_el.text if res_el is not None else "PT60M"
                step_minutes = 15 if resolution == "PT15M" else 60

                point_map: dict[int, float] = {}
                for point in period.iter(f"{{{_ENTSOE_NS}}}Point"):
                    pos_el = point.find(f"{{{_ENTSOE_NS}}}position")
                    price_el = point.find(f"{{{_ENTSOE_NS}}}price.amount")
                    if pos_el is not None and price_el is not None:
                        try:
                            point_map[int(pos_el.text)] = float(price_el.text)
                        except (ValueError, TypeError):
                            continue

                if not point_map:
                    continue

                max_pos = max(point_map.keys())
                last_price = 0.0
                for pos in range(1, max_pos + 1):
                    if pos in point_map:
                        last_price = point_map[pos]

                    ts_time = period_start + timedelta(minutes=(pos - 1) * step_minutes)
                    points.append(
                        {
                            "time": ts_time,
                            "node_id": zone_id,
                            "market_type": "ENTSOE_DAM",
                            "lmp_price_mwh": last_price,
                            "energy_component_mwh": last_price,
                            "congestion_component_mwh": None,
                            "loss_component_mwh": None,
                            "ghg_adder_mwh": None,
                            "price_confidence": None,
                            "forecast_horizon_minutes": None,
                        }
                    )

        return points


def _safe_float(value: Optional[str]) -> Optional[float]:
    """Safely coerce a string to float, returning None on empty or invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None
