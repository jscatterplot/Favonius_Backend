"""Unit tests for Analytics Service module - basic version."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

from src.websocket_handler.analytics_service import AnalyticsService
from src.websocket_handler.config import TimescaleConfig


class TestAnalyticsService:
    """Test AnalyticsService class."""
    
    @pytest.fixture
    def mock_timescale_config(self):
        """Mock TimescaleDB configuration."""
        config = Mock(spec=TimescaleConfig)
        config.host = "localhost"
        config.port = 5432
        config.database = "testdb"
        config.user = "test"
        config.password = "test"
        return config
    
    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        return client
    
    @pytest.fixture
    def analytics_service(self, mock_timescale_config):
        """Create AnalyticsService instance."""
        return AnalyticsService(config=mock_timescale_config)
    
    @pytest.mark.timeout(10)
    def test_analytics_service_initialization(self, mock_timescale_config):
        """Test AnalyticsService initialization."""
        service = AnalyticsService(config=mock_timescale_config)
        
        assert service.config == mock_timescale_config
        assert service.timescale_client is None
        assert service.logger is not None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_initialize(self, analytics_service, mock_timescale_client):
        """Test analytics service initialization."""
        with patch('src.websocket_handler.analytics_service.TimescaleClient', return_value=mock_timescale_client):
            await analytics_service.initialize()
            
            assert analytics_service.timescale_client == mock_timescale_client
            mock_timescale_client.connect.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_close(self, analytics_service, mock_timescale_client):
        """Test analytics service close."""
        analytics_service.timescale_client = mock_timescale_client
        
        await analytics_service.close()
        
        mock_timescale_client.disconnect.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_close_no_client(self, analytics_service):
        """Test analytics service close when no client."""
        analytics_service.timescale_client = None
        
        # Should not raise an exception
        await analytics_service.close()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_performance_analytics_basic(self, analytics_service, mock_timescale_client):
        """Test getting performance analytics with basic mocking."""
        analytics_service.timescale_client = mock_timescale_client
        
        fleet_operator_id = "FLEET_001"
        start_time = datetime.now(timezone.utc) - timedelta(days=7)
        end_time = datetime.now(timezone.utc)
        
        # Mock internal methods to return simple data
        with patch.object(analytics_service, '_calculate_uptime_metrics', return_value={"uptime": 0.95}) as mock_uptime, \
             patch.object(analytics_service, '_calculate_efficiency_metrics', return_value={"efficiency": 0.88}) as mock_efficiency, \
             patch.object(analytics_service, '_calculate_reliability_metrics', return_value={"reliability": 0.92}) as mock_reliability:
            
            result = await analytics_service.get_performance_analytics(
                fleet_operator_id, start_time, end_time
            )
            
            assert result is not None
            assert "uptime" in result
            assert "efficiency" in result
            assert "reliability" in result
            assert result["uptime"]["uptime"] == 0.95
            assert result["efficiency"]["efficiency"] == 0.88
            assert result["reliability"]["reliability"] == 0.92
    
    @pytest.mark.timeout(10)
    def test_analytics_service_without_config(self):
        """Test AnalyticsService initialization without config."""
        with pytest.raises(TypeError):
            AnalyticsService()
    
    @pytest.mark.timeout(10)
    def test_analytics_service_config_validation(self, mock_timescale_config):
        """Test analytics service config validation."""
        service = AnalyticsService(config=mock_timescale_config)
        
        assert service.config.host == "localhost"
        assert service.config.port == 5432
        assert service.config.database == "testdb"
        assert service.config.user == "test"
        assert service.config.password == "test"
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_analytics_service_initialization_success(self, analytics_service):
        """Test successful analytics service initialization."""
        with patch('src.websocket_handler.analytics_service.TimescaleClient') as mock_client_class:
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client_class.return_value = mock_client
            
            await analytics_service.initialize()
            
            assert analytics_service.timescale_client == mock_client
            mock_client.connect.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_analytics_service_disconnect_success(self, analytics_service):
        """Test successful analytics service disconnect."""
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        analytics_service.timescale_client = mock_client
        
        await analytics_service.close()
        
        mock_client.disconnect.assert_called_once()
        assert analytics_service.timescale_client == mock_client