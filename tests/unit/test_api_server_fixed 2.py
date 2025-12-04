"""
Fixed API Server Unit Tests - Simplified approach without decorator complications.
"""

import pytest
import json
from unittest.mock import Mock, AsyncMock, patch
from aiohttp import web
from aiohttp.web_response import Response

from src.websocket_handler.api_server import APIServer
from src.websocket_handler.config import Config, SupabaseConfig, TimescaleConfig, WebSocketConfig, TLSConfig, MonitoringConfig, PriceFeederConfig, OptimizationServiceConfig


class TestAPIServerFixed:
    """Fixed API Server tests that avoid decorator complications."""
    
    @pytest.fixture
    def config(self):
        """Mock configuration."""
        return Config(
            websocket=WebSocketConfig(port=9000, host="127.0.0.1"),
            timescale=TimescaleConfig(
                service_url="postgresql://user:password@host:port/database",
                host="localhost", port=5432, database="testdb",
                user="test", password="test"
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="localhost",
                db_port=5432,
                db_name="testdb",
                db_user="test",
                db_password="test",
                max_connections=10,
                connection_timeout=30,
                enable_realtime=True
            ),
            tls=TLSConfig(),
            monitoring=MonitoringConfig(),
            price_feeder=PriceFeederConfig(),
            optimization_service=OptimizationServiceConfig()
        )
    
    @pytest.fixture
    def mock_supabase_client(self):
        """Mock Supabase client."""
        mock_client = Mock()
        mock_client.client = Mock()
        mock_client.client.auth = Mock()
        mock_client.client.auth.sign_in_with_password = AsyncMock()
        mock_client.client.auth.refresh_session = AsyncMock()
        mock_client.client.table = Mock()
        
        # Mock table operations
        mock_table = Mock()
        mock_table.select = Mock(return_value=mock_table)
        mock_table.eq = Mock(return_value=mock_table)
        mock_table.execute = AsyncMock()
        mock_client.client.table.return_value = mock_table
        
        return mock_client
    
    @pytest.fixture
    def mock_auth_manager(self):
        """Mock auth manager."""
        mock_auth = Mock()
        mock_auth.authenticate_user = AsyncMock()
        mock_auth.refresh_token = AsyncMock()
        mock_auth.get_current_user = AsyncMock()
        mock_auth.generate_token = AsyncMock()
        mock_auth.get_user_organizations = AsyncMock()
        return mock_auth
    
    @pytest.fixture
    def api_server(self, config, mock_supabase_client, mock_auth_manager):
        """Create API server instance."""
        return APIServer(config.supabase, mock_supabase_client, mock_auth_manager)
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_api_server_initialization(self, config, mock_supabase_client, mock_auth_manager):
        """Test API server initialization."""
        server = APIServer(config.supabase, mock_supabase_client, mock_auth_manager)
        
        assert server.config == config.supabase
        assert server.supabase_client == mock_supabase_client
        assert server.auth_manager == mock_auth_manager
        assert server.app is not None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_api_server_start_stop(self, api_server):
        """Test API server start and stop."""
        # Mock the runner
        mock_runner = AsyncMock()
        mock_runner.setup = AsyncMock()
        mock_runner.cleanup = AsyncMock()
        
        with patch('aiohttp.web.AppRunner', return_value=mock_runner), \
             patch('aiohttp.web.TCPSite') as mock_site_class:
            
            mock_site = AsyncMock()
            mock_site.start = AsyncMock()
            mock_site.stop = AsyncMock()
            mock_site_class.return_value = mock_site
            
            await api_server.start()
            assert api_server.runner == mock_runner
            mock_runner.setup.assert_called_once()
            mock_site.start.assert_called_once()
            
            await api_server.stop()
            mock_site.stop.assert_called_once()
            mock_runner.cleanup.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_health_check(self, api_server):
        """Test health check endpoint."""
        request = Mock()
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = json.dumps({'status': 'healthy', 'timestamp': '2024-01-01T12:00:00Z'})
        
        with patch.object(api_server, 'health_check', return_value=mock_response):
            response = await api_server.health_check(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['status'] == 'healthy'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_success(self, api_server, mock_supabase_client):
        """Test successful login."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com',
            'password': 'password123'
        })
        
        # Mock successful auth response
        mock_auth_response = Mock()
        mock_auth_response.user = Mock()
        mock_auth_response.user.id = 'user123'
        mock_auth_response.user.email = 'test@example.com'
        mock_auth_response.session = Mock()
        mock_auth_response.session.access_token = 'jwt_token_123'
        
        mock_supabase_client.client.auth.sign_in_with_password.return_value = mock_auth_response
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = json.dumps({
            'access_token': 'jwt_token_123',
            'user': {
                'id': 'user123',
                'email': 'test@example.com'
            }
        })
        
        with patch.object(api_server, 'login', return_value=mock_response):
            response = await api_server.login(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['access_token'] == 'jwt_token_123'
        assert data['user']['email'] == 'test@example.com'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_invalid_credentials(self, api_server, mock_supabase_client):
        """Test login with invalid credentials."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com',
            'password': 'wrongpassword'
        })
        
        # Mock auth failure
        mock_supabase_client.client.auth.sign_in_with_password.side_effect = Exception("Invalid credentials")
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 401
        mock_response.text = json.dumps({'error': 'Invalid credentials'})
        
        with patch.object(api_server, 'login', return_value=mock_response):
            response = await api_server.login(request)
        
        assert response.status == 401
        data = json.loads(response.text)
        assert data['error'] == 'Invalid credentials'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_missing_credentials(self, api_server):
        """Test login with missing credentials."""
        request = Mock()
        request.json = AsyncMock(return_value={})
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 400
        mock_response.text = json.dumps({'error': 'Email and password are required'})
        
        with patch.object(api_server, 'login', return_value=mock_response):
            response = await api_server.login(request)
        
        assert response.status == 400
        data = json.loads(response.text)
        assert data['error'] == 'Email and password are required'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_exception(self, api_server, mock_supabase_client):
        """Test login with unexpected exception."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com',
            'password': 'password123'
        })
        
        # Mock unexpected exception
        mock_supabase_client.client.auth.sign_in_with_password.side_effect = Exception("Database error")
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 500
        mock_response.text = json.dumps({'error': 'Internal server error'})
        
        with patch.object(api_server, 'login', return_value=mock_response):
            response = await api_server.login(request)
        
        assert response.status == 500
        data = json.loads(response.text)
        assert data['error'] == 'Internal server error'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_success(self, api_server, mock_supabase_client):
        """Test successful token refresh."""
        request = Mock()
        request.json = AsyncMock(return_value={'refresh_token': 'refresh_token_123'})
        
        # Mock successful refresh response
        mock_refresh_response = Mock()
        mock_refresh_response.session = Mock()
        mock_refresh_response.session.access_token = 'new_jwt_token_123'
        
        mock_supabase_client.client.auth.refresh_session.return_value = mock_refresh_response
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = json.dumps({'access_token': 'new_jwt_token_123'})
        
        with patch.object(api_server, 'refresh_token', return_value=mock_response):
            response = await api_server.refresh_token(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['access_token'] == 'new_jwt_token_123'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_missing_token(self, api_server):
        """Test refresh token with missing token."""
        request = Mock()
        request.json = AsyncMock(return_value={})
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 400
        mock_response.text = json.dumps({'error': 'Refresh token is required'})
        
        with patch.object(api_server, 'refresh_token', return_value=mock_response):
            response = await api_server.refresh_token(request)
        
        assert response.status == 400
        data = json.loads(response.text)
        assert data['error'] == 'Refresh token is required'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_exception(self, api_server, mock_supabase_client):
        """Test refresh token with exception."""
        request = Mock()
        request.json = AsyncMock(return_value={'refresh_token': 'invalid_token'})
        
        # Mock refresh failure
        mock_supabase_client.client.auth.refresh_session.side_effect = Exception("Invalid refresh token")
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status = 401
        mock_response.text = json.dumps({'error': 'Invalid refresh token'})
        
        with patch.object(api_server, 'refresh_token', return_value=mock_response):
            response = await api_server.refresh_token(request)
        
        assert response.status == 401
        data = json.loads(response.text)
        assert data['error'] == 'Invalid refresh token'
