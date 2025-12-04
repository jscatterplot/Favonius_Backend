"""Basic unit tests for API server module - testing core functionality only."""

import asyncio
import pytest
import json
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

from src.websocket_handler.api_server import APIServer
from src.websocket_handler.config import SupabaseConfig
from src.websocket_handler.supabase_client import SupabaseClient
from src.websocket_handler.auth_manager import AuthManager


class TestAPIServerBasic:
    """Test the basic functionality of APIServer."""
    
    @pytest.fixture
    def mock_config(self):
        """Mock Supabase configuration."""
        config = Mock(spec=SupabaseConfig)
        config.url = "https://test.supabase.co"
        config.anon_key = "test_anon_key"
        config.service_key = "test_service_key"
        return config
    
    @pytest.fixture
    def mock_supabase_client(self):
        """Mock Supabase client."""
        client = Mock(spec=SupabaseClient)
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.health_check = AsyncMock(return_value='healthy')
        
        # Mock the client.client.auth methods
        client.client = Mock()
        client.client.auth = Mock()
        client.client.auth.sign_in_with_password = Mock()
        client.client.auth.refresh_session = Mock()
        
        return client
    
    @pytest.fixture
    def mock_auth_manager(self):
        """Mock auth manager."""
        manager = Mock(spec=AuthManager)
        manager.generate_token = Mock(return_value='jwt_token_123')
        return manager
    
    @pytest.fixture
    def api_server(self, mock_config, mock_supabase_client, mock_auth_manager):
        """Create API server instance."""
        return APIServer(mock_config, mock_supabase_client, mock_auth_manager)
    
    @pytest.mark.timeout(10)
    def test_api_server_initialization(self, mock_config, mock_supabase_client, mock_auth_manager):
        """Test API server initialization."""
        server = APIServer(mock_config, mock_supabase_client, mock_auth_manager)
        
        assert server.config == mock_config
        assert server.supabase_client == mock_supabase_client
        assert server.auth_manager == mock_auth_manager
        assert server.app is not None
        assert server.runner is None
        assert server.site is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_api_server(self, api_server):
        """Test API server startup."""
        with patch('aiohttp.web.AppRunner') as mock_runner_class, \
             patch('aiohttp.web.TCPSite') as mock_site_class:
            
            mock_runner = AsyncMock()
            mock_site = AsyncMock()
            mock_runner_class.return_value = mock_runner
            mock_site_class.return_value = mock_site
            
            await api_server.start(host='127.0.0.1', port=8080)
            
            assert api_server.runner == mock_runner
            assert api_server.site == mock_site
            mock_runner.setup.assert_called_once()
            mock_site.start.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_stop_api_server(self, api_server):
        """Test API server shutdown."""
        mock_runner = AsyncMock()
        mock_site = AsyncMock()
        api_server.runner = mock_runner
        api_server.site = mock_site
        
        await api_server.stop()
        
        mock_site.stop.assert_called_once()
        mock_runner.cleanup.assert_called_once()
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_health_check(self, api_server):
        """Test health check endpoint."""
        request = Mock()
        request.headers = {}
        
        response = await api_server.health_check(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['status'] == 'healthy'
        assert 'timestamp' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_success(self, api_server, mock_supabase_client, mock_auth_manager):
        """Test successful login."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com',
            'password': 'password123'
        })
        
        # Mock Supabase auth response
        mock_user = Mock()
        mock_user.id = 'user123'
        mock_user.email = 'test@example.com'
        mock_user.user_metadata = {'organization_id': 'org1', 'role': 'user'}
        
        mock_response = Mock()
        mock_response.user = mock_user
        mock_supabase_client.client.auth.sign_in_with_password.return_value = mock_response
        
        response = await api_server.login(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['access_token'] == 'jwt_token_123'
        assert data['user']['email'] == 'test@example.com'
        assert data['user']['id'] == 'user123'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_invalid_credentials(self, api_server, mock_supabase_client):
        """Test login with invalid credentials."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com',
            'password': 'wrongpassword'
        })
        
        # Mock Supabase auth response with no user
        mock_response = Mock()
        mock_response.user = None
        mock_supabase_client.client.auth.sign_in_with_password.return_value = mock_response
        
        response = await api_server.login(request)
        
        assert response.status == 401
        data = json.loads(response.text)
        assert data['error'] == 'Invalid credentials'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_success(self, api_server, mock_supabase_client, mock_auth_manager):
        """Test successful token refresh."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'refresh_token': 'refresh_token_123'
        })
        
        # Mock Supabase auth response
        mock_user = Mock()
        mock_user.id = 'user123'
        mock_user.user_metadata = {'organization_id': 'org1', 'role': 'user'}
        
        mock_response = Mock()
        mock_response.user = mock_user
        mock_supabase_client.client.auth.refresh_session.return_value = mock_response
        
        response = await api_server.refresh_token(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['access_token'] == 'jwt_token_123'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_missing_credentials(self, api_server):
        """Test login with missing credentials."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com'
            # Missing password
        })
        
        response = await api_server.login(request)
        
        assert response.status == 400
        data = json.loads(response.text)
        assert data['error'] == 'Email and password required'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_missing_token(self, api_server):
        """Test refresh token with missing token."""
        request = Mock()
        request.json = AsyncMock(return_value={})
        
        response = await api_server.refresh_token(request)
        
        assert response.status == 400
        data = json.loads(response.text)
        assert data['error'] == 'Refresh token required'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_health_check_exception(self, api_server, mock_supabase_client):
        """Test health check endpoint with exception."""
        request = Mock()
        request.headers = {}
        
        mock_supabase_client.health_check.side_effect = Exception("Health check failed")
        
        response = await api_server.health_check(request)
        
        assert response.status == 500
        data = json.loads(response.text)
        assert data['status'] == 'unhealthy'
        assert 'error' in data
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_exception(self, api_server, mock_supabase_client):
        """Test login with exception."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'email': 'test@example.com',
            'password': 'password123'
        })
        
        mock_supabase_client.client.auth.sign_in_with_password.side_effect = Exception("Auth failed")
        
        response = await api_server.login(request)
        
        assert response.status == 500
        data = json.loads(response.text)
        assert data['error'] == 'Login failed'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_exception(self, api_server, mock_supabase_client):
        """Test refresh token with exception."""
        request = Mock()
        request.json = AsyncMock(return_value={
            'refresh_token': 'refresh_token_123'
        })
        
        mock_supabase_client.client.auth.refresh_session.side_effect = Exception("Refresh failed")
        
        response = await api_server.refresh_token(request)
        
        assert response.status == 500
        data = json.loads(response.text)
        assert data['error'] == 'Token refresh failed'
