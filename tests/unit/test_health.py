"""Unit tests for health check server module."""

import asyncio
import pytest
import json
import time
from unittest.mock import Mock, AsyncMock, patch

from src.websocket_handler.health import HealthCheckServer


class TestHealthCheckServer:
    """Test the HealthCheckServer class."""
    
    @pytest.fixture
    def health_server(self):
        """Create health check server instance."""
        return HealthCheckServer(port=8081)
    
    @pytest.mark.timeout(10)
    def test_health_server_initialization(self):
        """Test health check server initialization."""
        server = HealthCheckServer(port=8081)
        
        assert server.port == 8081
        assert server.app is not None
        assert server.runner is None
        assert server.site is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_health_server(self, health_server):
        """Test health check server startup."""
        with patch('aiohttp.web.AppRunner') as mock_runner_class, \
             patch('aiohttp.web.TCPSite') as mock_site_class:
            
            mock_runner = AsyncMock()
            mock_site = AsyncMock()
            mock_runner_class.return_value = mock_runner
            mock_site_class.return_value = mock_site
            
            await health_server.start()
            
            assert health_server.runner == mock_runner
            assert health_server.site == mock_site
            mock_runner.setup.assert_called_once()
            mock_site.start.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_stop_health_server(self, health_server):
        """Test health check server shutdown."""
        mock_runner = AsyncMock()
        mock_site = AsyncMock()
        health_server.runner = mock_runner
        health_server.site = mock_site
        
        await health_server.stop()
        
        mock_site.stop.assert_called_once()
        mock_runner.cleanup.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_health_check_endpoint(self, health_server):
        """Test health check endpoint."""
        request = Mock()
        
        with patch('src.websocket_handler.health.health_checker') as mock_health_checker:
            mock_health_checker.run_checks = AsyncMock(return_value={
                'status': 'healthy',
                'timestamp': '2024-01-01T12:00:00Z',
                'checks': {
                    'connections': {'status': 'healthy'},
                    'database': {'status': 'healthy'},
                    'websocket': {'status': 'healthy'}
                }
            })
            
            response = await health_server._health_check(request)
            
            assert response.status == 200
            data = json.loads(response.text)
            assert data['status'] == 'healthy'
            assert 'timestamp' in data
            assert 'checks' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_readiness_check_endpoint(self, health_server):
        """Test readiness check endpoint."""
        request = Mock()
        
        with patch('src.websocket_handler.health.health_checker') as mock_health_checker:
            mock_health_checker.run_checks = AsyncMock(return_value={
                'status': 'healthy',
                'timestamp': '2024-01-01T12:00:00Z',
                'checks': {
                    'connections': {'status': 'healthy'},
                    'database': {'status': 'healthy'},
                    'websocket': {'status': 'healthy'}
                }
            })
            
            response = await health_server._readiness_check(request)
            
            assert response.status == 200
            data = json.loads(response.text)
            assert data['status'] == 'ready'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_liveness_check_endpoint(self, health_server):
        """Test liveness check endpoint."""
        request = Mock()
        
        response = await health_server._liveness_check(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['status'] == 'alive'
        assert 'timestamp' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_status_endpoint(self, health_server):
        """Test status endpoint."""
        request = Mock()
        
        with patch('src.websocket_handler.health.health_checker') as mock_health_checker, \
             patch('src.websocket_handler.health.metrics_collector') as mock_metrics_collector:
            
            mock_health_checker.run_checks = AsyncMock(return_value={
                'status': 'healthy',
                'timestamp': '2024-01-01T12:00:00Z',
                'checks': {
                    'connections': {'status': 'healthy'},
                    'database': {'status': 'healthy'},
                    'websocket': {'status': 'healthy'}
                }
            })
            
            mock_health_checker.get_last_results = Mock(return_value={
                'last_check': '2024-01-01T12:00:00Z',
                'status': 'healthy'
            })
            
            mock_metrics_collector.get_summary_stats = Mock(return_value={
                'active_connections': 5,
                'total_requests': 1000,
                'error_rate': 0.01
            })
            
            response = await health_server._status_endpoint(request)
            
            assert response.status == 200
            data = json.loads(response.text)
            assert data['service'] == 'websocket-handler'
            assert data['version'] == '1.0.0'
            assert 'timestamp' in data
            assert 'health' in data
            assert 'metrics' in data
            assert 'last_health_checks' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_metrics_summary_endpoint(self, health_server):
        """Test metrics summary endpoint."""
        request = Mock()
        
        with patch('src.websocket_handler.health.metrics_collector') as mock_metrics:
            mock_metrics.get_summary_stats = Mock(return_value={
                'websocket_connections': {
                    'active': 5,
                    'total': 100,
                    'peak': 20
                },
                'api_requests': {
                    'total': 1000,
                    'successful': 950,
                    'failed': 50,
                    'rate_per_minute': 10
                },
                'charging_sessions': {
                    'active': 3,
                    'completed_today': 15,
                    'total_energy_kwh': 250.5
                },
                'system_performance': {
                    'cpu_usage': 25.5,
                    'memory_usage': 512,
                    'disk_usage': 1024
                }
            })
            
            response = await health_server._metrics_summary(request)
            
            assert response.status == 200
            data = json.loads(response.text)
            assert 'metrics' in data
            assert 'timestamp' in data
            assert 'websocket_connections' in data['metrics']
            assert 'api_requests' in data['metrics']
            assert 'charging_sessions' in data['metrics']
            assert 'system_performance' in data['metrics']
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_health_check_unhealthy(self, health_server):
        """Test health check endpoint when system is unhealthy."""
        request = Mock()
        
        with patch('src.websocket_handler.health.health_checker') as mock_health_checker:
            mock_health_checker.run_checks = AsyncMock(return_value={
                'status': 'unhealthy',
                'timestamp': '2024-01-01T12:00:00Z',
                'checks': {
                    'connections': {'status': 'unhealthy', 'error': 'Connection failed'},
                    'database': {'status': 'healthy'},
                    'websocket': {'status': 'healthy'}
                }
            })
            
            response = await health_server._health_check(request)
            
            assert response.status == 503
            data = json.loads(response.text)
            assert data['status'] == 'unhealthy'
            assert 'timestamp' in data
            assert 'checks' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_readiness_check_not_ready(self, health_server):
        """Test readiness check endpoint when system is not ready."""
        request = Mock()
        
        with patch('src.websocket_handler.health.health_checker') as mock_health_checker:
            mock_health_checker.run_checks = AsyncMock(return_value={
                'status': 'unhealthy',
                'timestamp': '2024-01-01T12:00:00Z',
                'checks': {
                    'connections': {'status': 'unhealthy', 'error': 'Connection failed'},
                    'database': {'status': 'healthy'},
                    'websocket': {'status': 'healthy'}
                }
            })
            
            response = await health_server._readiness_check(request)
            
            assert response.status == 503
            data = json.loads(response.text)
            assert data['status'] == 'not_ready'
            assert 'checks' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_liveness_check_dead(self, health_server):
        """Test liveness check endpoint when system is dead."""
        request = Mock()
        
        # Liveness check always returns alive if server is responding
        response = await health_server._liveness_check(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['status'] == 'alive'
        assert 'timestamp' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_health_check_exception(self, health_server):
        """Test health check endpoint with exception."""
        request = Mock()
        
        with patch('src.websocket_handler.health.health_checker') as mock_health_checker:
            mock_health_checker.run_checks = AsyncMock(side_effect=Exception("Health check failed"))
            
            response = await health_server._health_check(request)
            
            assert response.status == 500
            data = json.loads(response.text)
            assert data['status'] == 'error'
            assert 'error' in data
            assert 'timestamp' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_metrics_summary_exception(self, health_server):
        """Test metrics summary endpoint with exception."""
        request = Mock()
        
        with patch('src.websocket_handler.health.metrics_collector') as mock_metrics:
            mock_metrics.get_summary_stats = Mock(side_effect=Exception("Metrics collection failed"))
            
            response = await health_server._metrics_summary(request)
            
            assert response.status == 500
            data = json.loads(response.text)
            assert 'error' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_with_exception(self):
        """Test health server startup with exception."""
        server = HealthCheckServer(port=8081)
        
        with patch('aiohttp.web.AppRunner') as mock_runner_class:
            mock_runner = AsyncMock()
            mock_runner_class.return_value = mock_runner
            mock_runner.setup.side_effect = Exception("Setup failed")
            
            with pytest.raises(Exception, match="Setup failed"):
                await server.start()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_stop_with_exception(self, health_server):
        """Test health server shutdown with exception."""
        mock_runner = AsyncMock()
        mock_site = AsyncMock()
        mock_site.stop.side_effect = Exception("Stop failed")
        health_server.runner = mock_runner
        health_server.site = mock_site
        
        # Should raise exception since stop method doesn't handle exceptions
        with pytest.raises(Exception, match="Stop failed"):
            await health_server.stop()
        
        mock_site.stop.assert_called_once()
        # cleanup should not be called if stop fails
        mock_runner.cleanup.assert_not_called()
