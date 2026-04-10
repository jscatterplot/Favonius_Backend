"""CAISO and ENTSO-E price feeder service."""

import asyncio
import contextlib
import csv
import io
import os
import defusedxml.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiohttp

from .config import PriceFeederConfig
from .monitoring import get_logger
from .timescale_client import TimescaleClient

# ENTSO-E constants
_ENTSOE_BASE_URL = "https://web-api.tp.entsoe.eu/api"
_ENTSOE_NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def _format_caiso_time(dt: datetime) -> str:
    """Format datetime for CAISO OASIS (YYYY-MM-DDTHH:MM-0000)."""
    return dt.strftime("%Y%m%dT%H:%M-0000")


def _format_entsoe_time(dt: datetime) -> str:
    """Format datetime for ENTSO-E API (YYYYMMddHHmm in UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d%H%M")


class PriceFeederService:
    """Fetches electricity price data from CAISO and ENTSO-E, stores in TimescaleDB.

    Handles both US (CAISO) and European (ENTSO-E) price feeds. European
    depots are detected by their timezone and prices are fetched from the
    ENTSO-E Transparency Platform.
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

        timeout = aiohttp.ClientTimeout(total=60)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        sources = []
        if self.config.nodes:
            sources.append(f"CAISO nodes: {', '.join(self.config.nodes)}")
        if self.config.entsoe_zones:
            sources.append(f"ENTSO-E zones: {', '.join(self.config.entsoe_zones)}")
        self.logger.info(
            "Price feeder started for %s with interval %ss",
            "; ".join(sources) if sources else "no configured sources",
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
        """Fetch price data for all configured nodes (CAISO + ENTSO-E) and store them."""
        if not self.session:
            return

        end_time = datetime.now(timezone.utc) + timedelta(hours=self.config.lookahead_hours)
        start_time = datetime.now(timezone.utc) - timedelta(hours=1)

        all_points: List[dict] = []

        # Fetch CAISO prices for configured US nodes
        for node in self.config.nodes:
            params = {
                "queryname": "PRC_LMP",
                "market_run_id": "DAM",
                "version": "12",
                "resultformat": "6",
                "node": node,
                "startdatetime": _format_caiso_time(start_time),
                "enddatetime": _format_caiso_time(end_time),
            }
            try:
                async with self.session.get(self.config.base_url, params=params) as response:
                    if response.status == 429:
                        # Rate limited for this node; log and skip until next interval.
                        self.logger.warning(
                            "CAISO rate limit hit (429) for node %s; skipping until next fetch window",
                            node,
                        )
                        continue
                    if response.status != 200:
                        raise RuntimeError(f"CAISO response code: {response.status}")
                    body = await response.read()
                    loop = asyncio.get_running_loop()
                    points = await loop.run_in_executor(None, self._parse_zip_response, body, node)
                    all_points.extend(points)
            except Exception as exc:
                self.logger.error(f"Failed to fetch prices for node {node}: {exc}")

        # Fetch ENTSO-E prices for configured European bidding zones
        if self.config.entsoe_zones and self._entsoe_token:
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

    def _parse_zip_response(self, content: bytes, node_id: str) -> List[dict]:
        """Parse zipped CSV content returned by CAISO."""
        points: List[dict] = []
        # Security: zip bomb protection
        _MAX_COMPRESSED_SIZE = 10 * 1024 * 1024  # 10 MB
        _MAX_DECOMPRESSED_SIZE = 100 * 1024 * 1024  # 100 MB
        if len(content) > _MAX_COMPRESSED_SIZE:
            self.logger.warning("CAISO zip file too large (%d bytes), skipping", len(content))
            return []
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # Check decompressed size before extraction
                total_size = sum(info.file_size for info in zf.infolist())
                if total_size > _MAX_DECOMPRESSED_SIZE:
                    self.logger.warning(
                        "CAISO zip decompressed size too large (%d bytes), skipping", total_size
                    )
                    return []
                for filename in zf.namelist():
                    if not filename.lower().endswith(".csv"):
                        continue
                    with zf.open(filename) as csvfile:
                        reader = csv.DictReader(io.TextIOWrapper(csvfile, encoding="utf-8"))
                        for row in reader:
                            try:
                                timestamp = datetime.fromisoformat(
                                    row["INTERVALSTARTTIME_GMT"].replace("Z", "+00:00")
                                )
                                points.append(
                                    {
                                        "time": timestamp,
                                        "node_id": node_id,
                                        "market_type": row.get("MARKET_RUN_ID", "DAM"),
                                        "lmp_price_mwh": _safe_float(row.get("LMP")),
                                        "energy_component_mwh": _safe_float(row.get("ENERGY")),
                                        "congestion_component_mwh": _safe_float(
                                            row.get("CONGESTION")
                                        ),
                                        "loss_component_mwh": _safe_float(row.get("LOSS")),
                                        "ghg_adder_mwh": _safe_float(row.get("GHG")),
                                        "price_confidence": None,
                                        "forecast_horizon_minutes": None,
                                    }
                                )
                            except Exception as exc:  # pragma: no cover - defensive
                                self.logger.debug(f"Failed to parse price row: {exc}")
                                continue
        except zipfile.BadZipFile:
            self.logger.error("CAISO response was not a valid ZIP archive")
        return points


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None
