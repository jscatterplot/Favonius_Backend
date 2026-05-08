"""Unit tests for the ENTSO-E price feeder service."""

import os

# Import price feeder
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import PriceFeederConfig
from websocket_handler.price_feeder import (
    PriceFeederService,
    _format_entsoe_time,
    _safe_float,
)


# Minimal-but-valid ENTSO-E ``Publication_MarketDocument`` payload covering
# two hourly points; used to exercise ``_parse_entsoe_xml`` without hitting
# the network.
_SAMPLE_ENTSOE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-05-08T00:00Z</start>
        <end>2026-05-08T02:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point>
        <position>1</position>
        <price.amount>92.50</price.amount>
      </Point>
      <Point>
        <position>2</position>
        <price.amount>88.10</price.amount>
      </Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


class TestPriceFeederService:
    """Test PriceFeederService lifecycle and ENTSO-E fetch path."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_electricity_prices = AsyncMock()
        return client

    @pytest.fixture
    def mock_optimization_engine(self):
        """Mock optimization engine."""
        engine = Mock()
        engine.request_run = AsyncMock()
        return engine

    @pytest.fixture
    def price_feeder_config(self):
        """Create price feeder configuration."""
        return PriceFeederConfig(
            enabled=True,
            entsoe_zones=["10YLT-1001A0008Q"],
            fetch_interval_seconds=900,
            lookahead_hours=24,
        )

    @pytest.fixture
    def price_feeder(self, price_feeder_config, mock_timescale_client, monkeypatch):
        """Create PriceFeederService instance with a non-empty ENTSO-E token."""
        monkeypatch.setenv("EUROPEAN_ELECTRICITY_API", "test-token")
        return PriceFeederService(price_feeder_config, mock_timescale_client)

    @pytest.mark.asyncio
    async def test_price_feeder_initialization(self, price_feeder):
        """Initial state: not running, no session, ENTSO-E zones loaded from config."""
        assert price_feeder.config.enabled is True
        assert price_feeder.config.entsoe_zones == ["10YLT-1001A0008Q"]
        assert price_feeder._running is False
        assert price_feeder.session is None

    @pytest.mark.asyncio
    async def test_start_disabled_feeder(self, price_feeder):
        """Disabled feeder must not open a session or schedule a task."""
        price_feeder.config.enabled = False
        await price_feeder.start()
        assert price_feeder._running is False
        assert price_feeder.session is None

    @pytest.mark.asyncio
    async def test_start_enabled_feeder(self, price_feeder):
        """Enabled feeder opens a session and starts the run loop."""
        await price_feeder.start()
        try:
            assert price_feeder._running is True
            assert price_feeder.session is not None
            assert price_feeder._task is not None
        finally:
            await price_feeder.stop()

    @pytest.mark.asyncio
    async def test_stop_feeder(self, price_feeder):
        """Stop tears down the session and cancels the run task."""
        await price_feeder.start()
        await price_feeder.stop()
        assert price_feeder._running is False
        assert price_feeder.session is None
        assert price_feeder._task is None

    @pytest.mark.asyncio
    async def test_set_optimization_engine(self, price_feeder, mock_optimization_engine):
        price_feeder.set_optimization_engine(mock_optimization_engine)
        assert price_feeder._optimization_engine == mock_optimization_engine

    @pytest.mark.asyncio
    async def test_trigger_fetch_manual(self, price_feeder):
        with patch.object(price_feeder, "_fetch_and_store_prices") as mock_fetch:
            await price_feeder.trigger_fetch()
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_fetch_swallows_errors(self, price_feeder):
        """Manual fetch must not raise — caller is fire-and-forget."""
        with patch.object(
            price_feeder, "_fetch_and_store_prices", side_effect=Exception("Fetch error")
        ):
            await price_feeder.trigger_fetch()

    @pytest.mark.asyncio
    async def test_fetch_no_session_short_circuits(self, price_feeder):
        """Without an aiohttp session, fetch returns silently without storing anything."""
        price_feeder.session = None
        await price_feeder._fetch_and_store_prices()
        price_feeder.timescale_client.store_electricity_prices.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_no_zones_short_circuits(
        self, price_feeder_config, mock_timescale_client, monkeypatch
    ):
        """Empty ENTSO-E zone list must skip the network call cleanly."""
        monkeypatch.setenv("EUROPEAN_ELECTRICITY_API", "test-token")
        price_feeder_config.entsoe_zones = []
        feeder = PriceFeederService(price_feeder_config, mock_timescale_client)
        feeder.session = Mock()
        await feeder._fetch_and_store_prices()
        mock_timescale_client.store_electricity_prices.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_no_token_short_circuits(
        self, price_feeder_config, mock_timescale_client, monkeypatch
    ):
        """Missing ENTSO-E token must skip the network call cleanly."""
        monkeypatch.delenv("EUROPEAN_ELECTRICITY_API", raising=False)
        feeder = PriceFeederService(price_feeder_config, mock_timescale_client)
        feeder.session = Mock()
        await feeder._fetch_and_store_prices()
        mock_timescale_client.store_electricity_prices.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_and_store_prices_success(
        self, price_feeder, mock_optimization_engine
    ):
        """A successful ENTSO-E response is parsed, stored, and triggers the optimizer."""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=_SAMPLE_ENTSOE_XML)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = Mock()
        mock_session.get.return_value = mock_ctx

        price_feeder.session = mock_session
        price_feeder._optimization_engine = mock_optimization_engine

        await price_feeder._fetch_and_store_prices()

        price_feeder.timescale_client.store_electricity_prices.assert_called_once()
        stored = price_feeder.timescale_client.store_electricity_prices.call_args[0][0]
        assert len(stored) == 2
        assert stored[0]["node_id"] == "10YLT-1001A0008Q"
        assert stored[0]["market_type"] == "ENTSOE_DAM"
        assert stored[0]["lmp_price_mwh"] == pytest.approx(92.50)
        assert stored[1]["lmp_price_mwh"] == pytest.approx(88.10)
        mock_optimization_engine.request_run.assert_called_once_with("price_update")

    @pytest.mark.asyncio
    async def test_fetch_http_error_logs_but_does_not_raise(self, price_feeder):
        """A non-200 response must be logged and swallowed; the loop keeps running."""
        mock_response = Mock()
        mock_response.status = 500
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = Mock()
        mock_session.get.return_value = mock_ctx

        price_feeder.session = mock_session
        await price_feeder._fetch_and_store_prices()
        price_feeder.timescale_client.store_electricity_prices.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_no_data_does_not_call_store(self, price_feeder):
        """An empty XML body must not trigger a store call."""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = AsyncMock(
            return_value='<?xml version="1.0"?><Publication_MarketDocument '
            'xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"/>'
        )
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = Mock()
        mock_session.get.return_value = mock_ctx

        price_feeder.session = mock_session
        await price_feeder._fetch_and_store_prices()
        price_feeder.timescale_client.store_electricity_prices.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_loop_swallows_errors(self, price_feeder):
        """The background loop must not crash if a single fetch raises."""
        with patch.object(
            price_feeder, "_fetch_and_store_prices", side_effect=Exception("Test error")
        ):
            price_feeder._running = True

            async def one_iteration():
                try:
                    await price_feeder._fetch_and_store_prices()
                except Exception as exc:  # noqa: BLE001
                    price_feeder.logger.error(f"Price feeder loop error: {exc}")

            await one_iteration()
            assert price_feeder._running is True

    def test_parse_entsoe_xml_success(self, price_feeder):
        """Two-point ENTSO-E response → two points with hourly stride."""
        points = price_feeder._parse_entsoe_xml(_SAMPLE_ENTSOE_XML, "10YLT-1001A0008Q")
        assert len(points) == 2
        assert points[0]["time"] == datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc)
        assert points[1]["time"] == datetime(2026, 5, 8, 1, 0, tzinfo=timezone.utc)
        assert points[0]["lmp_price_mwh"] == pytest.approx(92.50)
        assert points[1]["lmp_price_mwh"] == pytest.approx(88.10)
        assert all(p["node_id"] == "10YLT-1001A0008Q" for p in points)
        assert all(p["market_type"] == "ENTSOE_DAM" for p in points)

    def test_parse_entsoe_xml_invalid(self, price_feeder):
        """Malformed XML returns an empty list (logs the error)."""
        points = price_feeder._parse_entsoe_xml("<not-valid", "10YLT-1001A0008Q")
        assert points == []

    def test_parse_entsoe_xml_pt15m_resolution(self, price_feeder):
        """``PT15M`` resolution must produce 15-minute strides."""
        xml_15m = _SAMPLE_ENTSOE_XML.replace("PT60M", "PT15M")
        points = price_feeder._parse_entsoe_xml(xml_15m, "10YLT-1001A0008Q")
        assert len(points) == 2
        # 15-min stride: pos=2 lands at start + 15 min.
        assert points[1]["time"] == datetime(2026, 5, 8, 0, 15, tzinfo=timezone.utc)


class TestPriceFeederUtilities:
    """Test price feeder utility functions."""

    def test_format_entsoe_time(self):
        """ENTSO-E expects YYYYMMddHHmm in UTC."""
        dt = datetime(2026, 5, 8, 12, 30, 0, tzinfo=timezone.utc)
        assert _format_entsoe_time(dt) == "202605081230"

    def test_format_entsoe_time_converts_to_utc(self):
        """Naive or non-UTC timestamps must be converted to UTC before formatting."""
        # timezone-naive — module accepts and formats verbatim.
        naive = datetime(2026, 5, 8, 12, 30, 0)
        assert _format_entsoe_time(naive) == "202605081230"

    def test_safe_float_valid(self):
        assert _safe_float("100.5") == 100.5
        assert _safe_float("0") == 0.0
        assert _safe_float("-50.25") == -50.25

    def test_safe_float_invalid(self):
        assert _safe_float("") is None
        assert _safe_float(None) is None
        assert _safe_float("invalid") is None

    def test_safe_float_edge_cases(self):
        assert _safe_float("0.0") == 0.0
        assert _safe_float("1e5") == 100000.0
        assert _safe_float("1.23e-4") == 0.000123


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
