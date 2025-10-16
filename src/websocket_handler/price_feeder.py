"""CAISO price feeder service."""

import asyncio
import contextlib
import csv
import io
import zipfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiohttp

from .config import PriceFeederConfig
from .monitoring import get_logger
from .timescale_client import TimescaleClient


def _format_caiso_time(dt: datetime) -> str:
    """Format datetime for CAISO OASIS (YYYY-MM-DDTHH:MM-0000)."""
    return dt.strftime("%Y%m%dT%H:%M-0000")


class PriceFeederService:
    """Fetches electricity price data from CAISO and stores it in TimescaleDB."""

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
        self.logger.info(
            "Price feeder started for nodes %s with interval %ss",
            ", ".join(self.config.nodes),
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
        """Background loop for periodic fetching."""
        await asyncio.sleep(5)  # small delay to allow startup
        while self._running:
            try:
                await self._fetch_and_store_prices()
            except Exception as exc:
                self.logger.error(f"Price feeder loop error: {exc}")
            await asyncio.sleep(self.config.fetch_interval_seconds)

    async def _fetch_and_store_prices(self) -> None:
        """Fetch price data for all configured nodes and store them."""
        if not self.session:
            return

        end_time = datetime.now(timezone.utc) + timedelta(hours=self.config.lookahead_hours)
        start_time = datetime.now(timezone.utc) - timedelta(hours=1)

        all_points: List[dict] = []
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
                    if response.status != 200:
                        raise RuntimeError(f"CAISO response code: {response.status}")
                    body = await response.read()
                    points = self._parse_zip_response(body, node)
                    all_points.extend(points)
            except Exception as exc:
                self.logger.error(f"Failed to fetch prices for node {node}: {exc}")

        if not all_points:
            self.logger.warning("No price data fetched in this interval")
            return

        await self.timescale_client.store_electricity_prices(all_points)
        self.logger.info("Stored %d electricity price points", len(all_points))

        if self._optimization_engine:
            await self._optimization_engine.request_run("price_update")

    def _parse_zip_response(self, content: bytes, node_id: str) -> List[dict]:
        """Parse zipped CSV content returned by CAISO."""
        points: List[dict] = []
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for filename in zf.namelist():
                    if not filename.lower().endswith(".csv"):
                        continue
                    with zf.open(filename) as csvfile:
                        reader = csv.DictReader(io.TextIOWrapper(csvfile, encoding="utf-8"))
                        for row in reader:
                            try:
                                timestamp = datetime.fromisoformat(row["INTERVALSTARTTIME_GMT"].replace("Z", "+00:00"))
                                points.append(
                                    {
                                        "time": timestamp,
                                        "node_id": node_id,
                                        "market_type": row.get("MARKET_RUN_ID", "DAM"),
                                        "lmp_price_mwh": _safe_float(row.get("LMP")),
                                        "energy_component_mwh": _safe_float(row.get("ENERGY")),
                                        "congestion_component_mwh": _safe_float(row.get("CONGESTION")),
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
