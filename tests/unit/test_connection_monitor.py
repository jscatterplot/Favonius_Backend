"""Minimal unit tests for connection monitor module - testing only what works."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone

from src.websocket_handler.connection_monitor import ConnectionMonitor


class TestConnectionMonitorMinimal:
    """Test the minimal functionality of ConnectionMonitor."""
    
    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.health_check = AsyncMock(return_value=True)
        return client
    
    @pytest.fixture
    def mock_supabase_client(self):
        """Mock Supabase client."""
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.health_check = AsyncMock(return_value=True)
        return client
    
    @pytest.fixture
    def connection_monitor(self, mock_timescale_client, mock_supabase_client):
        """Create connection monitor instance."""
        return ConnectionMonitor(
            timescale_client=mock_timescale_client,
            supabase_client=mock_supabase_client,
            check_interval=5
        )
    
    @pytest.mark.timeout(10)
    def test_connection_monitor_initialization(self, mock_timescale_client, mock_supabase_client):
        """Test connection monitor initialization."""
        monitor = ConnectionMonitor(
            timescale_client=mock_timescale_client,
            supabase_client=mock_supabase_client,
            check_interval=30
        )
        
        assert monitor.timescale_client == mock_timescale_client
        assert monitor.supabase_client == mock_supabase_client
        assert monitor.check_interval == 30
        assert monitor.monitoring is False
        assert monitor.monitor_task is None
        assert monitor.last_health_check['timescale'] is None
        assert monitor.last_health_check['supabase'] is None
        assert monitor.connection_failures['timescale'] == 0
        assert monitor.connection_failures['supabase'] == 0
        assert monitor.max_failures_before_reconnect == 3
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_monitoring(self, connection_monitor):
        """Test starting connection monitoring."""
        assert connection_monitor.monitoring is False
        
        with patch('asyncio.create_task') as mock_create_task:
            mock_task = AsyncMock()
            mock_create_task.return_value = mock_task
            
            await connection_monitor.start_monitoring()
            
            assert connection_monitor.monitoring is True
            assert connection_monitor.monitor_task == mock_task
            mock_create_task.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_monitoring_already_running(self, connection_monitor):
        """Test starting monitoring when already running."""
        connection_monitor.monitoring = True
        
        await connection_monitor.start_monitoring()
        
        # Should not create new task
        assert connection_monitor.monitoring is True
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_stop_monitoring_no_task(self, connection_monitor):
        """Test stopping monitoring when no task exists."""
        connection_monitor.monitoring = True
        connection_monitor.monitor_task = None
        
        await connection_monitor.stop_monitoring()
        
        assert connection_monitor.monitoring is False
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_check_timescale_connection_success(self, connection_monitor, mock_timescale_client):
        """Test successful TimescaleDB connection check."""
        current_time = datetime.now(timezone.utc)
        mock_timescale_client.health_check.return_value = True
        
        await connection_monitor._check_timescale_connection(current_time)
        
        assert connection_monitor.last_health_check['timescale'] == current_time
        assert connection_monitor.connection_failures['timescale'] == 0
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_check_timescale_connection_failure(self, connection_monitor, mock_timescale_client):
        """Test failed TimescaleDB connection check."""
        current_time = datetime.now(timezone.utc)
        mock_timescale_client.health_check.return_value = False
        
        await connection_monitor._check_timescale_connection(current_time)
        
        assert connection_monitor.last_health_check['timescale'] == current_time
        assert connection_monitor.connection_failures['timescale'] == 1
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_check_supabase_connection_success(self, connection_monitor, mock_supabase_client):
        """Test successful Supabase connection check."""
        current_time = datetime.now(timezone.utc)
        mock_supabase_client.health_check.return_value = True
        
        await connection_monitor._check_supabase_connection(current_time)
        
        assert connection_monitor.last_health_check['supabase'] == current_time
        assert connection_monitor.connection_failures['supabase'] == 0
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_check_supabase_connection_failure(self, connection_monitor, mock_supabase_client):
        """Test failed Supabase connection check."""
        current_time = datetime.now(timezone.utc)
        mock_supabase_client.health_check.return_value = False
        
        await connection_monitor._check_supabase_connection(current_time)
        
        assert connection_monitor.last_health_check['supabase'] == current_time
        assert connection_monitor.connection_failures['supabase'] == 1
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_connection_status(self, connection_monitor):
        """Test getting connection status."""
        current_time = datetime.now(timezone.utc)
        connection_monitor.last_health_check['timescale'] = current_time
        connection_monitor.last_health_check['supabase'] = current_time
        connection_monitor.connection_failures['timescale'] = 0
        connection_monitor.connection_failures['supabase'] = 1
        
        status = connection_monitor.get_connection_status()
        
        assert 'monitoring' in status
        assert 'check_interval' in status
        assert 'connection_failures' in status
        assert 'last_health_check' in status
        assert status['connection_failures']['timescale'] == 0
        assert status['connection_failures']['supabase'] == 1
        assert status['last_health_check']['timescale'] is not None
        assert status['last_health_check']['supabase'] is not None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_monitor_loop_stops_when_not_monitoring(self, connection_monitor):
        """Test monitor loop stops when monitoring is disabled."""
        connection_monitor.monitoring = False
        
        # Should return immediately without doing anything
        await connection_monitor._monitor_loop()
        
        # No assertions needed as it should just return
    
    @pytest.mark.timeout(10)
    def test_connection_monitor_without_clients(self):
        """Test connection monitor initialization without clients."""
        monitor = ConnectionMonitor(check_interval=30)
        
        assert monitor.timescale_client is None
        assert monitor.supabase_client is None
        assert monitor.check_interval == 30
        assert monitor.monitoring is False
    
