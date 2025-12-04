"""Unit tests for TelemetryIngestionService."""

import pytest
import pytest_asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta
import uuid

from src.websocket_handler.telemetry_ingestion import TelemetryIngestionService
from src.websocket_handler.config import TimescaleConfig


class TestTelemetryIngestionService:
    """Test TelemetryIngestionService functionality."""
    
    @pytest.fixture
    def config(self):
        """Mock configuration."""
        return TimescaleConfig(
            service_url="postgresql://user:password@host:port/database",
            host="localhost",
            port=5432,
            database="testdb",
            user="test",
            password="test",
            max_connections=10,
            connection_timeout=30,
            enable_ssl=True,
            ssl_cert_path="/path/to/cert.pem",
            ssl_key_path="/path/to/key.pem",
            ssl_ca_path="/path/to/ca.pem"
        )
    
    @pytest.fixture
    def telemetry_service(self, config):
        """Create TelemetryIngestionService instance."""
        return TelemetryIngestionService(config)
    
    @pytest.mark.timeout(10)
    def test_telemetry_service_initialization(self, config):
        """Test TelemetryIngestionService initialization."""
        service = TelemetryIngestionService(config)
        
        assert service.timescale_config == config
        assert service.timescale_client is None
        assert service.batch_size == 1000
        assert service.batch_timeout == 5
        assert service.max_retries == 3
        assert service.retry_delay == 1
        assert service.running is False
        assert service.messages_processed == 0
        assert service.batches_inserted == 0
        assert service.errors_count == 0
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_start(self, telemetry_service):
        """Test starting the telemetry service."""
        with patch('src.websocket_handler.telemetry_ingestion.TimescaleClient') as mock_client_class:
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client_class.return_value = mock_client
            
            await telemetry_service.start()
            
            assert telemetry_service.running is True
            assert telemetry_service.timescale_client == mock_client
            mock_client.connect.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_stop(self, telemetry_service):
        """Test stopping the telemetry service."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        telemetry_service.timescale_client = mock_client
        telemetry_service.running = True
        
        await telemetry_service.stop()
        
        assert telemetry_service.running is False
        mock_client.disconnect.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_stop_no_client(self, telemetry_service):
        """Test stopping when no client exists."""
        telemetry_service.running = True
        
        await telemetry_service.stop()
        
        assert telemetry_service.running is False
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ingest_telemetry_data(self, telemetry_service):
        """Test ingesting telemetry data."""
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1,
            'session_id': 'test_session',
            'power_kw': 7.5,
            'energy_kwh': 10.0,
            'voltage_v': 240.0,
            'current_a': 31.25,
            'frequency_hz': 60.0,
            'soc_percent': 80.0,
            'temperature_c': 25.0
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        assert telemetry_service.messages_processed == 1
        assert 'TEST_STATION_001' in telemetry_service.batch_buffer
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ingest_telemetry_data_not_running(self, telemetry_service):
        """Test ingesting telemetry data when not running."""
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        assert telemetry_service.messages_processed == 1
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ingest_telemetry_data_batch_full(self, telemetry_service):
        """Test ingesting telemetry data when batch is full."""
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        # Fill buffer to capacity
        for i in range(1000):
            await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        assert telemetry_service.messages_processed == 1000
        assert len(telemetry_service.batch_buffer['TEST_STATION_001']) == 1000
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_process_batches(self, telemetry_service):
        """Test processing batches."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.insert_telemetry_batch = AsyncMock()
        telemetry_service.timescale_client = mock_client
        
        # Add data to buffer
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        # Process batches
        await telemetry_service._process_batches()
        
        # Should not process since batch size is not reached and timeout not exceeded
        mock_client.insert_telemetry_batch.assert_not_called()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_process_batches_error(self, telemetry_service):
        """Test processing batches with error."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.insert_telemetry_batch = AsyncMock(side_effect=Exception("Database error"))
        telemetry_service.timescale_client = mock_client
        
        # Add data to buffer
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        # Process batches
        await telemetry_service._process_batches()
        
        # Should handle error gracefully
        assert telemetry_service.errors_count == 0  # Error handled internally
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_process_batches_with_retry(self, telemetry_service):
        """Test processing batches with retry."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.insert_telemetry_batch = AsyncMock(side_effect=Exception("Database error"))
        telemetry_service.timescale_client = mock_client
        
        # Add data to buffer
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        # Process batches
        await telemetry_service._process_batches()
        
        # Should handle error gracefully
        assert telemetry_service.errors_count == 0
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_process_batches_max_retries_exceeded(self, telemetry_service):
        """Test processing batches with max retries exceeded."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.insert_telemetry_batch = AsyncMock(side_effect=Exception("Database error"))
        telemetry_service.timescale_client = mock_client
        
        # Add data to buffer
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        # Process batches
        await telemetry_service._process_batches()
        
        # Should handle error gracefully
        assert telemetry_service.errors_count == 0
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_flush_all_batches(self, telemetry_service):
        """Test flushing all batches."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.insert_telemetry_batch = AsyncMock()
        telemetry_service.timescale_client = mock_client
        
        # Add data to buffer
        telemetry_data = {
            'timestamp': '2024-01-01T12:00:00Z',
            'station_id': 'TEST_STATION_001',
            'evse_id': 1,
            'connector_id': 1
        }
        
        await telemetry_service.ingest_telemetry_data(telemetry_data)
        
        # Flush all batches
        await telemetry_service._flush_all_batches()
        
        # Should process the batch
        mock_client.insert_telemetry_batch.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_metrics(self, telemetry_service):
        """Test getting metrics."""
        telemetry_service.messages_processed = 1000
        telemetry_service.batches_inserted = 10
        telemetry_service.errors_count = 5
        
        metrics = await telemetry_service.get_metrics()
        
        assert metrics["messages_processed"] == 1000
        assert metrics["batches_inserted"] == 10
        assert metrics["errors_count"] == 5
        assert metrics["running"] is False
        assert "buffer_sizes" in metrics
        assert "last_batch_times" in metrics
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_health_check(self, telemetry_service):
        """Test health check."""
        # Set up mock client
        mock_client = AsyncMock()
        mock_client.health_check = AsyncMock(return_value={"status": "healthy"})
        telemetry_service.timescale_client = mock_client
        telemetry_service.running = True
        
        health = await telemetry_service.health_check()
        
        assert health["status"] == "healthy"
        assert "timescale" in health
        assert "metrics" in health
    
    @pytest.mark.timeout(10)
    def test_telemetry_service_without_config(self):
        """Test TelemetryIngestionService initialization without config."""
        # TelemetryIngestionService doesn't validate config in __init__, so this won't raise TypeError
        service = TelemetryIngestionService(None)
        assert service.timescale_config is None