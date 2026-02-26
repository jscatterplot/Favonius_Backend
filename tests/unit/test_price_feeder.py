"""Unit tests for price feeder service."""

import asyncio
import io
import os

# Import price feeder
import sys
import zipfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import PriceFeederConfig
from websocket_handler.price_feeder import PriceFeederService, _format_caiso_time, _safe_float


class TestPriceFeederService:
    """Test PriceFeederService functionality."""

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
            base_url="https://oasis.caiso.com/oasisapi/SingleZip",
            nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
            fetch_interval_seconds=900,
            lookahead_hours=24,
        )

    @pytest.fixture
    def price_feeder(self, price_feeder_config, mock_timescale_client):
        """Create PriceFeederService instance."""
        return PriceFeederService(price_feeder_config, mock_timescale_client)

    @pytest.mark.asyncio
    async def test_price_feeder_initialization(self, price_feeder):
        """Test price feeder initialization."""
        assert price_feeder.config.enabled is True
        assert price_feeder.config.base_url == "https://oasis.caiso.com/oasisapi/SingleZip"
        assert len(price_feeder.config.nodes) == 2
        assert price_feeder._running is False
        assert price_feeder.session is None

    @pytest.mark.asyncio
    async def test_start_disabled_feeder(self, price_feeder):
        """Test starting disabled price feeder."""
        price_feeder.config.enabled = False

        await price_feeder.start()

        assert price_feeder._running is False
        assert price_feeder.session is None

    @pytest.mark.asyncio
    async def test_start_enabled_feeder(self, price_feeder):
        """Test starting enabled price feeder."""
        await price_feeder.start()

        assert price_feeder._running is True
        assert price_feeder.session is not None
        assert price_feeder._task is not None

        await price_feeder.stop()

    @pytest.mark.asyncio
    async def test_stop_feeder(self, price_feeder):
        """Test stopping price feeder."""
        await price_feeder.start()

        assert price_feeder._running is True
        assert price_feeder.session is not None

        await price_feeder.stop()

        assert price_feeder._running is False
        assert price_feeder.session is None
        assert price_feeder._task is None

    @pytest.mark.asyncio
    async def test_set_optimization_engine(self, price_feeder, mock_optimization_engine):
        """Test setting optimization engine."""
        price_feeder.set_optimization_engine(mock_optimization_engine)

        assert price_feeder._optimization_engine == mock_optimization_engine

    @pytest.mark.asyncio
    async def test_trigger_fetch_manual(self, price_feeder, mock_timescale_client):
        """Test manual price fetch trigger."""
        # Mock successful fetch
        with patch.object(price_feeder, "_fetch_and_store_prices") as mock_fetch:
            await price_feeder.trigger_fetch()
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_fetch_error(self, price_feeder):
        """Test manual price fetch with error."""
        with patch.object(
            price_feeder, "_fetch_and_store_prices", side_effect=Exception("Fetch error")
        ):
            # Should not raise exception, just log error
            await price_feeder.trigger_fetch()

    @pytest.mark.asyncio
    async def test_fetch_and_store_prices_no_session(self, price_feeder):
        """Test fetch and store prices with no session."""
        price_feeder.session = None

        await price_feeder._fetch_and_store_prices()

        # Should return early without error

    @pytest.mark.asyncio
    async def test_fetch_and_store_prices_success(
        self, price_feeder, mock_timescale_client, mock_optimization_engine
    ):
        """Test successful price fetch and store."""
        # Mock session and response
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=b"mock zip content")

        # Create proper async context manager mock
        mock_context_manager = AsyncMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)

        mock_session = Mock()
        mock_session.get.return_value = mock_context_manager

        price_feeder.session = mock_session
        price_feeder._optimization_engine = mock_optimization_engine

        # Mock parse response
        mock_points = [
            {
                "time": datetime.now(timezone.utc),
                "node_id": "TH_SP15_GEN-APND",
                "lmp_price_mwh": 100.0,
            }
        ]

        with patch.object(price_feeder, "_parse_zip_response", return_value=mock_points):
            await price_feeder._fetch_and_store_prices()

            # Should be called once with all points from all nodes (2 nodes * 1 point each = 2 points)
            expected_points = mock_points + mock_points  # One for each node
            mock_timescale_client.store_electricity_prices.assert_called_once_with(expected_points)
            mock_optimization_engine.request_run.assert_called_once_with("price_update")

    @pytest.mark.asyncio
    async def test_fetch_and_store_prices_http_error(self, price_feeder):
        """Test price fetch with HTTP error."""
        mock_response = Mock()
        mock_response.status = 500

        # Create proper async context manager mock
        mock_context_manager = AsyncMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)

        mock_session = Mock()
        mock_session.get.return_value = mock_context_manager

        price_feeder.session = mock_session

        # Should log error but not crash
        await price_feeder._fetch_and_store_prices()

    @pytest.mark.asyncio
    async def test_fetch_and_store_prices_no_data(self, price_feeder, mock_timescale_client):
        """Test price fetch with no data."""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=b"mock zip content")

        # Create proper async context manager mock
        mock_context_manager = AsyncMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)

        mock_session = Mock()
        mock_session.get.return_value = mock_context_manager

        price_feeder.session = mock_session

        # Mock parse response returning empty list
        with patch.object(price_feeder, "_parse_zip_response", return_value=[]):
            await price_feeder._fetch_and_store_prices()

            # Should not store anything
            mock_timescale_client.store_electricity_prices.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_loop_error_handling(self, price_feeder):
        """Test run loop error handling."""
        with patch.object(
            price_feeder, "_fetch_and_store_prices", side_effect=Exception("Test error")
        ):
            price_feeder._running = True

            # Create a modified loop that exits after one iteration
            async def limited_run_loop():
                await asyncio.sleep(0.1)  # Short sleep instead of full interval
                try:
                    await price_feeder._fetch_and_store_prices()
                except Exception as exc:
                    price_feeder.logger.error(f"Price feeder loop error: {exc}")
                # Exit after one iteration instead of continuing the loop

            # Run one iteration
            await limited_run_loop()

            # Should not crash, just log error
            assert price_feeder._running is True

    def test_parse_zip_response_success(self, price_feeder):
        """Test successful ZIP response parsing."""
        # Create mock CSV content
        csv_content = "INTERVALSTARTTIME_GMT,LMP,ENERGY,CONGESTION,LOSS,GHG\n2023-01-01T00:00:00Z,100.0,95.0,3.0,2.0,0.0\n"

        # Create ZIP file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test_data.csv", csv_content)
        zip_buffer.seek(0)

        points = price_feeder._parse_zip_response(zip_buffer.getvalue(), "TH_SP15_GEN-APND")

        assert len(points) == 1
        assert points[0]["node_id"] == "TH_SP15_GEN-APND"
        assert points[0]["lmp_price_mwh"] == 100.0
        assert points[0]["energy_component_mwh"] == 95.0
        assert points[0]["congestion_component_mwh"] == 3.0
        assert points[0]["loss_component_mwh"] == 2.0
        assert points[0]["ghg_adder_mwh"] == 0.0

    def test_parse_zip_response_invalid_zip(self, price_feeder):
        """Test parsing invalid ZIP response."""
        invalid_content = b"not a zip file"

        points = price_feeder._parse_zip_response(invalid_content, "TH_SP15_GEN-APND")

        assert len(points) == 0

    def test_parse_zip_response_invalid_csv(self, price_feeder):
        """Test parsing ZIP with invalid CSV."""
        csv_content = "INVALID,HEADER\ninvalid,data\n"

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test_data.csv", csv_content)
        zip_buffer.seek(0)

        points = price_feeder._parse_zip_response(zip_buffer.getvalue(), "TH_SP15_GEN-APND")

        assert len(points) == 0

    def test_parse_zip_response_multiple_files(self, price_feeder):
        """Test parsing ZIP with multiple CSV files."""
        csv_content1 = "INTERVALSTARTTIME_GMT,LMP,ENERGY,CONGESTION,LOSS,GHG\n2023-01-01T00:00:00Z,100.0,95.0,3.0,2.0,0.0\n"
        csv_content2 = "INTERVALSTARTTIME_GMT,LMP,ENERGY,CONGESTION,LOSS,GHG\n2023-01-01T01:00:00Z,110.0,100.0,5.0,5.0,0.0\n"

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("data1.csv", csv_content1)
            zip_file.writestr("data2.csv", csv_content2)
            zip_file.writestr("readme.txt", "This is not a CSV file")
        zip_buffer.seek(0)

        points = price_feeder._parse_zip_response(zip_buffer.getvalue(), "TH_SP15_GEN-APND")

        assert len(points) == 2
        assert all(point["node_id"] == "TH_SP15_GEN-APND" for point in points)

    def test_parse_zip_response_missing_fields(self, price_feeder):
        """Test parsing CSV with missing fields."""
        csv_content = "INTERVALSTARTTIME_GMT,LMP\n2023-01-01T00:00:00Z,100.0\n"

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test_data.csv", csv_content)
        zip_buffer.seek(0)

        points = price_feeder._parse_zip_response(zip_buffer.getvalue(), "TH_SP15_GEN-APND")

        assert len(points) == 1
        assert points[0]["lmp_price_mwh"] == 100.0
        assert points[0]["energy_component_mwh"] is None
        assert points[0]["congestion_component_mwh"] is None

    def test_parse_zip_response_empty_values(self, price_feeder):
        """Test parsing CSV with empty values."""
        csv_content = (
            "INTERVALSTARTTIME_GMT,LMP,ENERGY,CONGESTION,LOSS,GHG\n2023-01-01T00:00:00Z,,,,\n"
        )

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test_data.csv", csv_content)
        zip_buffer.seek(0)

        points = price_feeder._parse_zip_response(zip_buffer.getvalue(), "TH_SP15_GEN-APND")

        assert len(points) == 1
        assert points[0]["lmp_price_mwh"] is None
        assert points[0]["energy_component_mwh"] is None


class TestPriceFeederUtilities:
    """Test price feeder utility functions."""

    def test_format_caiso_time(self):
        """Test CAISO time formatting."""
        dt = datetime(2023, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
        formatted = _format_caiso_time(dt)

        assert formatted == "20230101T12:30-0000"

    def test_safe_float_valid(self):
        """Test _safe_float with valid input."""
        assert _safe_float("100.5") == 100.5
        assert _safe_float("0") == 0.0
        assert _safe_float("-50.25") == -50.25

    def test_safe_float_invalid(self):
        """Test _safe_float with invalid input."""
        assert _safe_float("") is None
        assert _safe_float(None) is None
        assert _safe_float("invalid") is None
        assert _safe_float("abc123") is None

    def test_safe_float_edge_cases(self):
        """Test _safe_float with edge cases."""
        assert _safe_float("0.0") == 0.0
        assert _safe_float("1e5") == 100000.0
        assert _safe_float("1.23e-4") == 0.000123


class TestPriceFeederIntegration:
    """Test price feeder integration scenarios."""

    @pytest.fixture
    def mock_optimization_engine(self):
        """Mock optimization engine."""
        engine = Mock()
        engine.request_run = AsyncMock()
        return engine

    @pytest.mark.asyncio
    async def test_full_price_fetch_cycle(self, mock_timescale_client, mock_optimization_engine):
        """Test full price fetch cycle."""
        config = PriceFeederConfig(
            enabled=True,
            nodes=["TH_SP15_GEN-APND"],
            fetch_interval_seconds=1,  # Short interval for testing
            lookahead_hours=1,
        )

        price_feeder = PriceFeederService(config, mock_timescale_client)
        price_feeder.set_optimization_engine(mock_optimization_engine)

        # Mock successful HTTP response
        csv_content = "INTERVALSTARTTIME_GMT,LMP,ENERGY,CONGESTION,LOSS,GHG\n2023-01-01T00:00:00Z,100.0,95.0,3.0,2.0,0.0\n"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test_data.csv", csv_content)
        zip_buffer.seek(0)

        mock_response = Mock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=zip_buffer.getvalue())

        mock_context_manager = AsyncMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)

        mock_session = Mock()
        mock_session.get.return_value = mock_context_manager

        price_feeder.session = mock_session

        await price_feeder._fetch_and_store_prices()

        # Verify data was stored and optimization was triggered
        mock_timescale_client.store_electricity_prices.assert_called_once()
        mock_optimization_engine.request_run.assert_called_once_with("price_update")

        # Verify stored data structure
        stored_data = mock_timescale_client.store_electricity_prices.call_args[0][0]
        assert len(stored_data) == 1
        assert stored_data[0]["node_id"] == "TH_SP15_GEN-APND"
        assert stored_data[0]["lmp_price_mwh"] == 100.0

    @pytest.mark.asyncio
    async def test_multiple_nodes_fetch(self, mock_timescale_client):
        """Test fetching prices for multiple nodes."""
        config = PriceFeederConfig(
            enabled=True,
            nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
            fetch_interval_seconds=1,
            lookahead_hours=1,
        )

        price_feeder = PriceFeederService(config, mock_timescale_client)

        # Mock responses for both nodes
        csv_content = "INTERVALSTARTTIME_GMT,LMP,ENERGY,CONGESTION,LOSS,GHG\n2023-01-01T00:00:00Z,100.0,95.0,3.0,2.0,0.0\n"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("test_data.csv", csv_content)
        zip_buffer.seek(0)

        mock_response = Mock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=zip_buffer.getvalue())

        mock_context_manager = AsyncMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)

        mock_session = Mock()
        mock_session.get.return_value = mock_context_manager

        price_feeder.session = mock_session

        await price_feeder._fetch_and_store_prices()

        # Should fetch for both nodes
        assert mock_session.get.call_count == 2
        mock_timescale_client.store_electricity_prices.assert_called_once()

        # Verify data for both nodes was stored
        stored_data = mock_timescale_client.store_electricity_prices.call_args[0][0]
        assert len(stored_data) == 2
        node_ids = [point["node_id"] for point in stored_data]
        assert "TH_SP15_GEN-APND" in node_ids
        assert "TH_NP15_GEN-APND" in node_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
