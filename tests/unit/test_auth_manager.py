"""
Unit tests for AuthManager - Authentication and authorization middleware.
"""

import pytest
import jwt
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

from src.websocket_handler.auth_manager import AuthManager, RateLimiter, require_auth, require_permission, require_role, rate_limit
from src.websocket_handler.config import SupabaseConfig


class TestAuthManager:
    """Test the AuthManager class."""
    
    @pytest.fixture
    def config(self):
        """Mock Supabase configuration."""
        return SupabaseConfig(
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
        )
    
    @pytest.fixture
    def mock_supabase_client(self):
        """Mock Supabase client."""
        mock_client = Mock()
        mock_client.client = Mock()
        mock_client.client.table = Mock()
        
        # Mock table responses
        mock_table = Mock()
        mock_table.select = Mock()
        mock_table.eq = Mock()
        mock_table.execute = Mock()
        
        mock_client.client.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        
        # Mock execute response
        mock_response = Mock()
        mock_response.data = [{'role': 'user', 'organizations': {'id': 'org1', 'name': 'Test Org'}}]
        mock_table.execute.return_value = mock_response
        
        return mock_client
    
    @pytest.fixture
    def auth_manager(self, config, mock_supabase_client):
        """Create AuthManager instance."""
        return AuthManager(config, mock_supabase_client)
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_auth_manager_initialization(self, config, mock_supabase_client):
        """Test AuthManager initialization."""
        manager = AuthManager(config, mock_supabase_client)
        
        assert manager.config == config
        assert manager.supabase_client == mock_supabase_client
        assert manager.jwt_secret == config.service_key
        assert manager.jwt_algorithm == "HS256"
        assert manager.token_expiry == timedelta(minutes=15)
        assert manager.user_cache == {}
        assert manager.cache_ttl == timedelta(minutes=5)
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_user_valid_token(self, auth_manager):
        """Test authentication with valid token."""
        user_id = "user123"
        token_payload = {
            'sub': user_id,
            'exp': datetime.now(timezone.utc) + timedelta(minutes=30),
            'iat': datetime.now(timezone.utc)
        }
        
        # Generate valid JWT token
        token = jwt.encode(token_payload, auth_manager.jwt_secret, algorithm=auth_manager.jwt_algorithm)
        
        # Mock user data from Supabase
        user_data = {
            'id': user_id,
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        # Mock the _get_user_from_supabase method
        with patch.object(auth_manager, '_get_user_from_supabase', new_callable=AsyncMock, return_value=user_data):
            result = await auth_manager.authenticate_user(token)
            
            assert result is not None
            assert result['id'] == user_id
            assert result['email'] == 'test@example.com'
            assert result['role'] == 'user'
            assert result['organization_id'] == 'org1'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_user_invalid_token(self, auth_manager):
        """Test authentication with invalid token."""
        invalid_token = "invalid.jwt.token"
        
        result = await auth_manager.authenticate_user(invalid_token)
        
        assert result is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_user_expired_token(self, auth_manager):
        """Test authentication with expired token."""
        user_id = "user123"
        token_payload = {
            'sub': user_id,
            'exp': datetime.now(timezone.utc) - timedelta(minutes=30),  # Expired
            'iat': datetime.now(timezone.utc) - timedelta(hours=1)
        }
        
        # Generate expired JWT token
        token = jwt.encode(token_payload, auth_manager.jwt_secret, algorithm=auth_manager.jwt_algorithm)
        
        result = await auth_manager.authenticate_user(token)
        
        assert result is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_user_cached(self, auth_manager):
        """Test authentication with cached user."""
        user_id = "user123"
        token_payload = {
            'sub': user_id,
            'exp': datetime.now(timezone.utc) + timedelta(minutes=30),
            'iat': datetime.now(timezone.utc)
        }
        
        token = jwt.encode(token_payload, auth_manager.jwt_secret, algorithm=auth_manager.jwt_algorithm)
        
        # Pre-populate cache
        cached_user = {
            'id': user_id,
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        auth_manager.user_cache[f"user:{user_id}"] = {
            'user': cached_user,
            'cached_at': datetime.now(timezone.utc)
        }
        
        result = await auth_manager.authenticate_user(token)
        
        assert result is not None
        assert result['id'] == user_id
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_user_from_supabase(self, auth_manager):
        """Test getting user from Supabase."""
        user_id = "user123"
        user_data = {
            'id': user_id,
            'email': 'test@example.com',
            'role': 'user',
            'organization_id': 'org1'
        }
        
        # Mock the Supabase response
        mock_response = Mock()
        mock_response.data = [{'role': 'user', 'organizations': {'id': 'org1', 'name': 'Test Org', 'type': 'fleet', 'subscription_tier': 'premium'}}]
        
        auth_manager.supabase_client.client.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_response
        
        result = await auth_manager._get_user_from_supabase(user_id)
        
        assert result is not None
        assert result['id'] == user_id
        assert result['role'] == 'user'
        assert result['organization_id'] == 'org1'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_user_from_supabase_not_found(self, auth_manager):
        """Test getting non-existent user from Supabase."""
        user_id = "nonexistent"
        
        # Mock empty Supabase response
        mock_response = Mock()
        mock_response.data = []
        
        auth_manager.supabase_client.client.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_response
        
        result = await auth_manager._get_user_from_supabase(user_id)
        
        assert result is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authorize_action_allowed(self, auth_manager):
        """Test authorization for allowed action."""
        user = {
            'id': 'user123',
            'role': 'admin',
            'organization_id': 'org1',
            'permissions': ['read', 'write', 'admin']
        }
        
        result = await auth_manager.authorize_action(user, 'stations', 'read')
        
        assert result is True
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authorize_action_denied(self, auth_manager):
        """Test authorization for denied action."""
        user = {
            'id': 'user123',
            'role': 'user',
            'organization_id': 'org1',
            'permissions': ['read']
        }
        
        result = await auth_manager.authorize_action(user, 'stations', 'admin')
        
        assert result is False
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_user_organizations(self, auth_manager):
        """Test getting user organizations."""
        user_id = "user123"
        organizations = [
            {'id': 'org1', 'name': 'Organization 1'},
            {'id': 'org2', 'name': 'Organization 2'}
        ]
        
        # Mock Supabase response
        mock_response = Mock()
        mock_response.data = [
            {'role': 'admin', 'organizations': {'id': 'org1', 'name': 'Organization 1', 'type': 'fleet', 'subscription_tier': 'premium'}},
            {'role': 'user', 'organizations': {'id': 'org2', 'name': 'Organization 2', 'type': 'fleet', 'subscription_tier': 'basic'}}
        ]
        
        auth_manager.supabase_client.client.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_response
        
        result = await auth_manager.get_user_organizations(user_id)
        
        assert len(result) == 2
        assert result[0]['id'] == 'org1'
        assert result[1]['id'] == 'org2'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_create_api_key(self, auth_manager):
        """Test creating API key."""
        user_id = "user123"
        name = "Test API Key"
        permissions = ["read", "write"]
        api_key = "api_key_123"
        
        result = await auth_manager.create_api_key(user_id, name, permissions)
        
        # Should return a JWT token
        assert result is not None
        assert isinstance(result, str)
        # JWT tokens have 3 parts separated by dots
        assert len(result.split('.')) == 3
    
    @pytest.mark.timeout(10)
    def test_generate_token(self, auth_manager):
        """Test token generation."""
        user_id = "user123"
        organization_id = "org1"
        role = "admin"
        
        token = auth_manager.generate_token(user_id, organization_id, role)
        
        assert token is not None
        # Decode token to verify contents
        payload = jwt.decode(token, auth_manager.jwt_secret, algorithms=[auth_manager.jwt_algorithm])
        assert payload['sub'] == user_id
        assert payload['organization_id'] == organization_id
        assert payload['role'] == role
    
    @pytest.mark.timeout(10)
    def test_verify_api_key_valid(self, auth_manager):
        """Test verifying valid API key."""
        user_id = "user123"
        api_key_payload = {
            'sub': user_id,
            'name': 'Test API Key',
            'permissions': ['read', 'write'],
            'type': 'api_key',
            'exp': datetime.now(timezone.utc) + timedelta(days=30)
        }
        
        # Generate valid API key
        api_key = jwt.encode(api_key_payload, auth_manager.jwt_secret, algorithm=auth_manager.jwt_algorithm)
        
        result = auth_manager.verify_api_key(api_key)
        
        assert result is not None
        assert result['id'] == user_id
        assert result['name'] == 'Test API Key'
        assert result['type'] == 'api_key'
        assert 'read' in result['permissions']
        assert 'write' in result['permissions']
    
    @pytest.mark.timeout(10)
    def test_verify_api_key_invalid(self, auth_manager):
        """Test verifying invalid API key."""
        api_key = "invalid_api_key"
        
        with patch.object(auth_manager.supabase_client, 'verify_api_key', return_value=None):
            result = auth_manager.verify_api_key(api_key)
        
        assert result is None
    
    @pytest.mark.timeout(10)
    def test_clear_user_cache(self, auth_manager):
        """Test clearing user cache."""
        user_id = "user123"
        auth_manager.user_cache[f"user:{user_id}"] = {'id': user_id, 'email': 'test@example.com'}
        
        auth_manager.clear_user_cache(user_id)
        
        assert f"user:{user_id}" not in auth_manager.user_cache
    
    @pytest.mark.timeout(10)
    def test_clear_all_cache(self, auth_manager):
        """Test clearing all cache."""
        auth_manager.user_cache["user:123"] = {'id': '123'}
        auth_manager.user_cache["user:456"] = {'id': '456'}
        
        auth_manager.clear_all_cache()
        
        assert len(auth_manager.user_cache) == 0


class TestRateLimiter:
    """Test the RateLimiter class."""
    
    @pytest.fixture
    def rate_limiter(self):
        """Create RateLimiter instance."""
        return RateLimiter()
    
    @pytest.mark.timeout(10)
    def test_rate_limiter_initialization(self):
        """Test RateLimiter initialization."""
        limiter = RateLimiter()
        
        assert limiter.requests == {}
        assert limiter.cleanup_interval == timedelta(minutes=5)
        assert isinstance(limiter.last_cleanup, datetime)
    
    @pytest.mark.timeout(10)
    def test_is_allowed_within_limit(self, rate_limiter):
        """Test rate limiting within allowed limit."""
        key = "test_key"
        limit = 10
        window = timedelta(minutes=1)
        
        # Make requests within limit
        for i in range(5):
            result = rate_limiter.is_allowed(key, limit, window)
            assert result is True
    
    @pytest.mark.timeout(10)
    def test_is_allowed_exceeds_limit(self, rate_limiter):
        """Test rate limiting when exceeding limit."""
        key = "test_key"
        limit = 3
        window = timedelta(minutes=1)
        
        # Make requests within limit
        for i in range(3):
            result = rate_limiter.is_allowed(key, limit, window)
            assert result is True
        
        # This should exceed the limit
        result = rate_limiter.is_allowed(key, limit, window)
        assert result is False
    
    @pytest.mark.timeout(10)
    def test_is_allowed_different_keys(self, rate_limiter):
        """Test rate limiting with different keys."""
        key1 = "key1"
        key2 = "key2"
        limit = 2
        window = timedelta(minutes=1)
        
        # Both keys should be allowed
        assert rate_limiter.is_allowed(key1, limit, window) is True
        assert rate_limiter.is_allowed(key2, limit, window) is True
        
        # Both keys should still be allowed
        assert rate_limiter.is_allowed(key1, limit, window) is True
        assert rate_limiter.is_allowed(key2, limit, window) is True
        
        # Both should now be denied
        assert rate_limiter.is_allowed(key1, limit, window) is False
        assert rate_limiter.is_allowed(key2, limit, window) is False


class TestAuthDecorators:
    """Test authentication decorators."""
    
    @pytest.fixture
    def mock_auth_manager(self):
        """Mock auth manager."""
        mock_auth = Mock()
        mock_auth.authenticate_user = AsyncMock()
        mock_auth.authorize_action = AsyncMock()
        return mock_auth
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_require_auth_decorator_success(self, mock_auth_manager):
        """Test require_auth decorator with successful authentication."""
        @require_auth(mock_auth_manager)
        async def protected_function(request, user=None):
            return {"status": "success"}
        
        # Mock request with valid token
        request = Mock()
        request.headers = {'Authorization': 'Bearer valid_token'}
        
        # Mock successful authentication
        mock_auth_manager.authenticate_user.return_value = {
            'id': 'user123',
            'email': 'test@example.com',
            'role': 'user'
        }
        
        result = await protected_function(request)
        
        assert result['status'] == 'success'
        mock_auth_manager.authenticate_user.assert_called_once_with('valid_token')
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_require_auth_decorator_failure(self, mock_auth_manager):
        """Test require_auth decorator with failed authentication."""
        @require_auth(mock_auth_manager)
        async def protected_function(request, user=None):
            return {"status": "success"}
        
        # Mock request with invalid token
        request = Mock()
        request.headers = {'Authorization': 'Bearer invalid_token'}
        
        # Mock failed authentication
        mock_auth_manager.authenticate_user.return_value = None
        
        result = await protected_function(request)
        
        # Decorator returns (response, status_code) tuple on failure
        assert isinstance(result, tuple)
        assert len(result) == 2
        response, status_code = result
        assert response['error'] == 'Invalid or expired token'
        assert status_code == 401
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_require_permission_decorator_success(self, mock_auth_manager):
        """Test require_permission decorator with successful authorization."""
        # Create a real AuthManager instance for the decorator
        from src.websocket_handler.config import SupabaseConfig
        config = SupabaseConfig(
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
        )
        
        # Mock the authorize_action method
        with patch.object(AuthManager, 'authorize_action', new_callable=AsyncMock) as mock_authorize:
            mock_authorize.return_value = True
            
            @require_permission('stations', 'read')
            async def protected_function(self, request, user=None):
                return {"status": "success"}
            
            # Mock request and user
            request = Mock()
            user = {'id': 'user123', 'role': 'admin', 'permissions': ['read', 'write']}
            
            # Create a mock object with auth_manager attribute
            mock_self = Mock()
            mock_self.auth_manager = Mock()
            mock_self.auth_manager.authorize_action = AsyncMock(return_value=True)
            
            result = await protected_function(mock_self, request, user=user)
            
            assert result['status'] == 'success'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_require_permission_decorator_failure(self, mock_auth_manager):
        """Test require_permission decorator with failed authorization."""
        # Mock the authorize_action method on the instance
        mock_auth_manager.authorize_action = AsyncMock(return_value=False)
        
        # Create a mock class instance with auth_manager attribute
        mock_instance = Mock()
        mock_instance.auth_manager = mock_auth_manager
        
        @require_permission('stations', 'admin')
        async def protected_function(request, user=None):
            return {"status": "success"}
        
        # Mock request and user
        request = Mock()
        user = {'id': 'user123', 'role': 'user', 'permissions': ['read']}
        
        result = await protected_function(mock_instance, request, user=user)
        
        # Decorator returns (response, status_code) tuple on failure
        assert isinstance(result, tuple)
        assert len(result) == 2
        response, status_code = result
        assert response['error'] == 'Insufficient permissions'
        assert status_code == 403
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_require_role_decorator_success(self, mock_auth_manager):
        """Test require_role decorator with successful role check."""
        @require_role(['admin', 'manager'])
        async def protected_function(request, user):
            return {"status": "success"}
        
        # Mock request and user
        request = Mock()
        user = {'id': 'user123', 'role': 'admin'}
        
        result = await protected_function(request, user=user)
        
        assert result['status'] == 'success'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_require_role_decorator_failure(self, mock_auth_manager):
        """Test require_role decorator with failed role check."""
        @require_role(['admin', 'manager'])
        async def protected_function(request, user):
            return {"status": "success"}
        
        # Mock request and user
        request = Mock()
        user = {'id': 'user123', 'role': 'user'}
        
        result = await protected_function(request, user=user)
        
        # Decorator returns (response, status_code) tuple on failure
        assert isinstance(result, tuple)
        assert len(result) == 2
        response, status_code = result
        assert response['error'] == 'Required role: admin, manager'
        assert status_code == 403
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_rate_limit_decorator_success(self, mock_auth_manager):
        """Test rate_limit decorator within limits."""
        limiter = RateLimiter()
        
        @rate_limit(limiter, 10, 60)
        async def rate_limited_function(request):
            return {"status": "success"}
        
        # Mock request
        request = Mock()
        request.remote = "192.168.1.1"
        
        result = await rate_limited_function(request)
        
        assert result['status'] == 'success'
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_rate_limit_decorator_exceeded(self, mock_auth_manager):
        """Test rate_limit decorator when limits are exceeded."""
        limiter = RateLimiter()
        
        @rate_limit(limiter, 1, 60)
        async def rate_limited_function(request):
            return {"status": "success"}
        
        # Mock request
        request = Mock()
        request.remote = "192.168.1.1"
        
        # First call should succeed
        result1 = await rate_limited_function(request)
        assert result1['status'] == 'success'
        
        # Second call should be rate limited
        result2 = await rate_limited_function(request)
        
        # Decorator returns (response, status_code) tuple on failure
        assert isinstance(result2, tuple)
        assert len(result2) == 2
        response, status_code = result2
        assert response['error'] == 'Rate limit exceeded'
        assert status_code == 429
