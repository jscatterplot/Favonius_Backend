"""Unit tests for API server module."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.websocket_handler.api_server import APIServer
from src.websocket_handler.auth_manager import AuthManager
from src.websocket_handler.config import SupabaseConfig
from src.websocket_handler.supabase_client import SupabaseClient


class TestAPIServer:
    """Test the APIServer class."""

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
        client.health_check = AsyncMock(return_value="healthy")

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
        manager.generate_token = Mock(return_value="jwt_token_123")
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
        with (
            patch("aiohttp.web.AppRunner") as mock_runner_class,
            patch("aiohttp.web.TCPSite") as mock_site_class,
        ):

            mock_runner = AsyncMock()
            mock_site = AsyncMock()
            mock_runner_class.return_value = mock_runner
            mock_site_class.return_value = mock_site

            await api_server.start(host="127.0.0.1", port=8080)

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
        assert data["status"] == "healthy"
        assert "timestamp" in data

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_success(self, api_server, mock_supabase_client, mock_auth_manager):
        """Test successful login."""
        request = Mock()
        request.json = AsyncMock(
            return_value={"email": "test@example.com", "password": "password123"}
        )

        # Mock Supabase auth response
        mock_user = Mock()
        mock_user.id = "user123"
        mock_user.email = "test@example.com"
        mock_user.user_metadata = {"organization_id": "org1", "role": "user"}

        mock_response = Mock()
        mock_response.user = mock_user
        mock_supabase_client.client.auth.sign_in_with_password.return_value = mock_response

        response = await api_server.login(request)

        assert response.status == 200
        data = json.loads(response.text)
        assert data["access_token"] == "jwt_token_123"
        assert data["user"]["email"] == "test@example.com"
        assert data["user"]["id"] == "user123"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_login_invalid_credentials(self, api_server, mock_supabase_client):
        """Test login with invalid credentials."""
        request = Mock()
        request.json = AsyncMock(
            return_value={"email": "test@example.com", "password": "wrongpassword"}
        )

        # Mock Supabase auth response with no user
        mock_response = Mock()
        mock_response.user = None
        mock_supabase_client.client.auth.sign_in_with_password.return_value = mock_response

        response = await api_server.login(request)

        assert response.status == 401
        data = json.loads(response.text)
        assert data["error"] == "Invalid credentials"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_refresh_token_success(self, api_server, mock_supabase_client, mock_auth_manager):
        """Test successful token refresh."""
        request = Mock()
        request.json = AsyncMock(return_value={"refresh_token": "refresh_token_123"})

        # Mock Supabase auth response
        mock_user = Mock()
        mock_user.id = "user123"
        mock_user.user_metadata = {"organization_id": "org1", "role": "user"}

        mock_response = Mock()
        mock_response.user = mock_user
        mock_supabase_client.client.auth.refresh_session.return_value = mock_response

        response = await api_server.refresh_token(request)

        assert response.status == 200
        data = json.loads(response.text)
        assert data["access_token"] == "jwt_token_123"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_current_user(self, api_server):
        """Test get current user endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        # Call the method directly with the user parameter
        response = await api_server.get_current_user(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert data["user"]["id"] == "user123"
        assert data["user"]["email"] == "test@example.com"
        assert data["user"]["role"] == "user"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_organizations(self, api_server, mock_auth_manager):
        """Test get organizations endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_auth_manager.get_user_organizations.return_value = [
            {"id": "org1", "name": "Organization 1"},
            {"id": "org2", "name": "Organization 2"},
        ]

        # Create a mock response that simulates what the endpoint would return
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = json.dumps(
            {
                "organizations": [
                    {"id": "org1", "name": "Organization 1"},
                    {"id": "org2", "name": "Organization 2"},
                ]
            }
        )

        # Call the method directly with the user parameter
        response = await api_server.get_organizations(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["organizations"]) == 2
        assert data["organizations"][0]["id"] == "org1"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_vehicles(self, api_server, mock_supabase_client):
        """Test get vehicles endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.get_vehicles_by_organization.return_value = [
            {"id": "vehicle1", "name": "Vehicle 1", "organization_id": "org1"},
            {"id": "vehicle2", "name": "Vehicle 2", "organization_id": "org1"},
        ]

        response = await api_server.get_vehicles(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["vehicles"]) == 2
        assert data["vehicles"][0]["name"] == "Vehicle 1"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_create_vehicle(self, api_server, mock_supabase_client):
        """Test create vehicle endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.json = AsyncMock(return_value={"name": "New Vehicle", "vehicle_type": "car"})

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.create_vehicle.return_value = {
            "id": "vehicle123",
            "name": "New Vehicle",
        }

        response = await api_server.create_vehicle(request, user)

        assert response.status == 201
        data = json.loads(response.text)
        assert data["vehicle"]["name"] == "New Vehicle"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_stations(self, api_server, mock_supabase_client):
        """Test get stations endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        # Mock the Supabase client chain calls
        mock_sites_response = Mock()
        mock_sites_response.data = [{"id": "site1"}, {"id": "site2"}]

        mock_stations_response = Mock()
        mock_stations_response.data = [
            {"id": "station1", "name": "Station 1", "site_id": "site1"},
            {"id": "station2", "name": "Station 2", "site_id": "site2"},
        ]

        # Mock the table chain
        mock_table = Mock()
        mock_table.select.return_value.eq.return_value.execute.return_value = mock_sites_response
        mock_table.select.return_value.in_.return_value.execute.return_value = (
            mock_stations_response
        )

        api_server.supabase_client.client.table.return_value = mock_table

        response = await api_server.get_stations(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["stations"]) == 2
        assert data["stations"][0]["name"] == "Station 1"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_sessions(self, api_server, mock_supabase_client):
        """Test get sessions endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.query = {"vehicle_id": "vehicle1", "limit": "10"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        # Mock the Supabase client response
        mock_response = Mock()
        mock_response.data = [
            {"id": "session1", "vehicle_id": "vehicle1", "status": "active"},
            {"id": "session2", "vehicle_id": "vehicle1", "status": "completed"},
        ]

        mock_table = Mock()
        mock_table.select.return_value.eq.return_value.execute.return_value = mock_response

        api_server.supabase_client.client.table.return_value = mock_table

        response = await api_server.get_sessions(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["sessions"]) == 2
        assert data["sessions"][0]["status"] == "active"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_active_sessions(self, api_server, mock_supabase_client):
        """Test get active sessions endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.get_active_sessions = AsyncMock(
            return_value=[
                {"id": "session1", "vehicle_id": "vehicle1", "status": "active"},
                {"id": "session2", "vehicle_id": "vehicle2", "status": "active"},
            ]
        )

        response = await api_server.get_active_sessions(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["sessions"]) == 2
        assert all(session["status"] == "active" for session in data["sessions"])

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_stop_session(self, api_server, mock_supabase_client):
        """Test stop session endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.match_info = {"session_id": "session1"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.update_session_status = AsyncMock(
            return_value={"id": "session1", "status": "stopped"}
        )

        response = await api_server.stop_session(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert data["session"]["status"] == "stopped"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_schedules(self, api_server, mock_supabase_client):
        """Test get schedules endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.query = {"vehicle_id": "vehicle1"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.get_schedule_configs = AsyncMock(
            return_value=[
                {"id": "schedule1", "vehicle_id": "vehicle1", "enabled": True},
                {"id": "schedule2", "vehicle_id": "vehicle1", "enabled": False},
            ]
        )

        response = await api_server.get_schedules(request, user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["schedules"]) == 2
        assert data["schedules"][0]["enabled"] is True

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_create_schedule(self, api_server, mock_supabase_client):
        """Test create schedule endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.json = AsyncMock(
            return_value={
                "vehicle_id": "vehicle1",
                "start_time": "08:00",
                "end_time": "18:00",
                "enabled": True,
            }
        )

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.create_schedule_config = AsyncMock(
            return_value={"id": "schedule123", "vehicle_id": "vehicle1", "enabled": True}
        )

        response = await api_server.create_schedule(request, user)

        assert response.status == 201
        data = json.loads(response.text)
        assert data["schedule"]["vehicle_id"] == "vehicle1"
        assert data["schedule"]["enabled"] is True

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_energy_analytics(self, api_server, mock_supabase_client):
        """Test get energy analytics endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.query = {"vehicle_id": "vehicle1", "period": "week"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.get_daily_energy_summary = AsyncMock(
            return_value=[
                {"date": "2024-01-01", "energy_kwh": 25.5},
                {"date": "2024-01-02", "energy_kwh": 30.2},
            ]
        )

        response = await api_server.get_energy_analytics(request, user=user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["analytics"]) == 2
        assert data["analytics"][0]["energy_kwh"] == 25.5

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_cost_analytics(self, api_server, mock_supabase_client):
        """Test get cost analytics endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.query = {"vehicle_id": "vehicle1", "period": "month"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        # Mock the Supabase client response
        mock_response = Mock()
        mock_response.data = [
            {"date": "2024-01-01", "cost_usd": 12.50},
            {"date": "2024-01-02", "cost_usd": 15.25},
        ]

        mock_table = Mock()
        mock_table.select.return_value.eq.return_value.gte.return_value.lte.return_value.execute.return_value = (
            mock_response
        )

        api_server.supabase_client.client.table.return_value = mock_table

        response = await api_server.get_cost_analytics(request, user=user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["analytics"]) == 2
        assert data["analytics"][0]["cost_usd"] == 12.50

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_savings_analytics(self, api_server, mock_supabase_client):
        """Test get savings analytics endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.query = {"vehicle_id": "vehicle1", "period": "year"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        mock_supabase_client.calculate_savings = AsyncMock(
            return_value=[
                {"date": "2024-01-01", "savings_usd": 5.25},
                {"date": "2024-01-02", "savings_usd": 7.50},
            ]
        )

        response = await api_server.get_savings_analytics(request, user=user)

        assert response.status == 200
        data = json.loads(response.text)
        assert len(data["savings"]) == 2
        assert data["savings"][0]["savings_usd"] == 5.25

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_subscribe_realtime(self, api_server):
        """Test realtime subscription endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}
        request.query = {"channel": "vehicle_updates"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        response = await api_server.subscribe_realtime(request, user=user)

        assert response.status == 200
        data = json.loads(response.text)
        assert "subscription_url" in data
        assert "channels" in data
        assert "fleet_updates" in data["channels"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_sync_status(self, api_server, mock_supabase_client):
        """Test get sync status endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        response = await api_server.get_sync_status(request, user=user)

        assert response.status == 200
        data = json.loads(response.text)
        assert data["sync_status"] == "running"
        assert "last_sync" in data

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_force_sync(self, api_server, mock_supabase_client):
        """Test force sync endpoint."""
        request = Mock()
        request.headers = {"Authorization": "Bearer access_token_123"}

        # Mock user data that would be injected by auth decorator
        user = {
            "id": "user123",
            "email": "test@example.com",
            "role": "user",
            "organization_id": "org1",
        }

        request.json = AsyncMock(return_value={"type": "all"})

        response = await api_server.force_sync(request, user=user)

        assert response.status == 200
        data = json.loads(response.text)
        assert "message" in data
        assert "timestamp" in data
