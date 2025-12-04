"""Simplified unit tests for API server module - focusing on core functionality."""

import asyncio
import pytest
import json
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

from src.websocket_handler.api_server import APIServer
from src.websocket_handler.config import SupabaseConfig
from src.websocket_handler.supabase_client import SupabaseClient
from src.websocket_handler.auth_manager import AuthManager


class TestAPIServerCore:
    """Test the core functionality of APIServer without decorators."""
    
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
        
        # Mock Supabase query methods
        client.query = AsyncMock()
        client.insert = AsyncMock()
        client.update = AsyncMock()
        client.delete = AsyncMock()
        client.get_organization = AsyncMock()
        client.update_organization = AsyncMock()
        client.get_vehicles_by_organization = AsyncMock()
        client.create_vehicle = AsyncMock()
        client.update_vehicle = AsyncMock()
        client.delete_vehicle = AsyncMock()
        client.get_stations_by_organization = AsyncMock()
        client.create_station = AsyncMock()
        client.update_station = AsyncMock()
        client.get_sessions_by_organization = AsyncMock()
        client.get_active_sessions_by_organization = AsyncMock()
        client.update_session = AsyncMock()
        client.get_schedules_by_organization = AsyncMock()
        client.create_schedule = AsyncMock()
        client.update_schedule = AsyncMock()
        client.delete_schedule = AsyncMock()
        client.get_energy_analytics = AsyncMock()
        client.get_cost_analytics = AsyncMock()
        client.get_savings_analytics = AsyncMock()
        client.get_sync_status = AsyncMock()
        client.force_sync = AsyncMock()
        
        return client
    
    @pytest.fixture
    def mock_auth_manager(self):
        """Mock auth manager."""
        manager = Mock(spec=AuthManager)
        manager.authenticate_user = AsyncMock()
        manager.refresh_token = AsyncMock()
        manager.get_current_user = AsyncMock()
        manager.generate_token = Mock(return_value='jwt_token_123')
        manager.get_user_organizations = AsyncMock()
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
    async def test_get_current_user_core(self, api_server):
        """Test get current user core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        # Test the core functionality by calling the method directly
        response = await api_server.get_current_user(request, user)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['user']['id'] == 'user123'
        assert data['user']['email'] == 'test@example.com'
        assert data['user']['role'] == 'user'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_organizations_core(self, api_server, mock_auth_manager):
        """Test get organizations core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        mock_auth_manager.get_user_organizations.return_value = [
            {'id': 'org1', 'name': 'Organization 1'},
            {'id': 'org2', 'name': 'Organization 2'}
        ]
        
        # Test the core functionality by calling the method directly
        response = await api_server.get_organizations(request, user)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert len(data['organizations']) == 2
        assert data['organizations'][0]['name'] == 'Organization 1'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_vehicles_core(self, api_server, mock_supabase_client):
        """Test get vehicles core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        mock_supabase_client.get_vehicles_by_organization.return_value = [
            {'id': 'vehicle1', 'name': 'Vehicle 1', 'organization_id': 'org1'},
            {'id': 'vehicle2', 'name': 'Vehicle 2', 'organization_id': 'org1'}
        ]
        
        # Test the core functionality by calling the method directly
        response = await api_server.get_vehicles(request, user)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert len(data['vehicles']) == 2
        assert data['vehicles'][0]['name'] == 'Vehicle 1'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_create_vehicle_core(self, api_server, mock_supabase_client):
        """Test create vehicle core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        request.json = AsyncMock(return_value={
            'name': 'New Vehicle',
            'vehicle_type': 'car'
        })
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        mock_supabase_client.create_vehicle.return_value = {'id': 'vehicle123', 'name': 'New Vehicle'}
        
        # Test the core functionality by calling the method directly
        response = await api_server.create_vehicle(request, user)
        
        assert response.status == 201
        data = json.loads(response.text)
        assert data['vehicle']['name'] == 'New Vehicle'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_stations_core(self, api_server, mock_supabase_client):
        """Test get stations core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        # Mock the Supabase client chain calls
        mock_sites_response = Mock()
        mock_sites_response.data = [{'id': 'site1'}, {'id': 'site2'}]
        
        mock_stations_response = Mock()
        mock_stations_response.data = [
            {'id': 'station1', 'name': 'Station 1', 'site_id': 'site1'},
            {'id': 'station2', 'name': 'Station 2', 'site_id': 'site2'}
        ]
        
        # Mock the table chain
        mock_table = Mock()
        mock_table.select.return_value.eq.return_value.execute.return_value = mock_sites_response
        mock_table.select.return_value.in_.return_value.execute.return_value = mock_stations_response
        
        api_server.supabase_client.client.table.return_value = mock_table
        
        # Test the core functionality by calling the method directly
        response = await api_server.get_stations(request, user)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert len(data['stations']) == 2
        assert data['stations'][0]['name'] == 'Station 1'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_sessions_core(self, api_server, mock_supabase_client):
        """Test get sessions core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        request.query = {'vehicle_id': 'vehicle1', 'limit': '10'}
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        # Mock the Supabase client response
        mock_response = Mock()
        mock_response.data = [
            {'id': 'session1', 'vehicle_id': 'vehicle1', 'status': 'active'},
            {'id': 'session2', 'vehicle_id': 'vehicle1', 'status': 'completed'}
        ]
        
        # Mock the query chain - need to handle the case where gte/lte might not be called
        mock_query = Mock()
        mock_query.execute.return_value = mock_response
        
        mock_table = Mock()
        mock_table.select.return_value.eq.return_value = mock_query
        
        api_server.supabase_client.client.table.return_value = mock_table
        
        # Test the core functionality by calling the method directly
        response = await api_server.get_sessions(request, user)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert len(data['sessions']) == 2
        assert data['sessions'][0]['status'] == 'active'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_energy_analytics_core(self, api_server, mock_supabase_client):
        """Test get energy analytics core functionality."""
        request = Mock()
        request.headers = {'Authorization': 'Bearer access_token_123'}
        request.query = {'vehicle_id': 'vehicle1', 'period': 'week'}
        
        # Mock user data that would be injected by auth decorator
        user = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        mock_supabase_client.get_daily_energy_summary = AsyncMock(return_value=[
            {'date': '2024-01-01', 'energy_kwh': 25.5},
            {'date': '2024-01-02', 'energy_kwh': 30.2}
        ])
        
        # Test the core functionality by calling the method directly
        response = await api_server.get_energy_analytics(request, user)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert len(data['analytics']) == 2
        assert data['analytics'][0]['energy_kwh'] == 25.5
