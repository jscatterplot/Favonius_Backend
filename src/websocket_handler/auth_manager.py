"""Authentication and authorization middleware for Supabase integration."""

import json
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, List, Optional

import jwt

from .cache_manager import CacheManager
from .config import SupabaseConfig
from .monitoring import get_logger
from .supabase_client import SupabaseClient


class AuthManager:
    """Authentication and authorization manager."""

    def __init__(
        self,
        config: SupabaseConfig,
        supabase_client: SupabaseClient,
        cache_manager: Optional[CacheManager] = None,
    ):
        """Initialize auth manager."""
        self.config = config
        self.supabase_client = supabase_client
        self.logger = get_logger(__name__)

        # Initialize cache manager
        self.cache_manager = cache_manager or CacheManager(
            max_size=1000, default_ttl=timedelta(seconds=300)
        )

        # JWT settings. WS_AUTH_JWT_SIGNING_KEY is required: silent fallback to
        # SUPABASE_SERVICE_KEY (the most privileged credential in the project)
        # would mean every WebSocket JWT is signed with a key that grants full
        # database access, so a single leaked WS token becomes a forgery oracle
        # for arbitrary credentials. Fail fast at construction instead.
        # ``strip()`` so a whitespace/newline-only value (a common copy-paste
        # mishap from secrets tooling) is treated the same as unset.
        raw_signing_key = os.environ.get("WS_AUTH_JWT_SIGNING_KEY", "")
        self.jwt_secret = raw_signing_key.strip()
        if not self.jwt_secret:
            raise RuntimeError(
                "WS_AUTH_JWT_SIGNING_KEY is required. Generate one with "
                "`openssl rand -base64 48` and set it on the WebSocket Handler "
                "service. Do NOT reuse SUPABASE_SERVICE_KEY — it has full "
                "database access and must not sign end-user JWTs."
            )
        raw_previous_secret = os.getenv("WS_AUTH_JWT_SIGNING_KEY_PREVIOUS", "")
        previous_secret = raw_previous_secret.strip()
        self.jwt_previous_secret = previous_secret or None
        self.jwt_algorithm = "HS256"
        self.token_expiry = timedelta(minutes=15)
        self.jwt_issuer = os.getenv("WS_AUTH_JWT_ISSUER", "favonius-websocket-handler")
        self.jwt_audience = os.getenv("WS_AUTH_JWT_AUDIENCE", "favonius-websocket-api")

        # Legacy cache for backward compatibility
        self.user_cache: Dict[str, Dict[str, Any]] = {}
        self.cache_ttl = timedelta(minutes=5)

    async def authenticate_user(self, token: str) -> Optional[Dict[str, Any]]:
        """Authenticate user from JWT token."""
        try:
            # Decode JWT token
            payload = self._decode_jwt(token)

            user_id = payload.get("sub")
            if not user_id:
                return None

            # Check cache first
            cache_key = f"user:{user_id}"
            cached_user = await self.cache_manager.get(cache_key)
            if cached_user:
                self.logger.debug(f"User {user_id} found in cache")
                return cached_user

            # Check legacy cache
            if cache_key in self.user_cache:
                cached_data = self.user_cache[cache_key]
                if datetime.now(timezone.utc) - cached_data["cached_at"] < self.cache_ttl:
                    self.logger.debug(f"User {user_id} found in legacy cache")
                    return cached_data["user"]

            # Get user from Supabase
            user_data = await self._get_user_from_supabase(user_id)
            if not user_data:
                return None

            # Cache user data in both caches
            await self.cache_manager.set(cache_key, user_data, ttl=timedelta(seconds=300))
            self.user_cache[cache_key] = {
                "user": user_data,
                "cached_at": datetime.now(timezone.utc),
            }

            return user_data

        except jwt.ExpiredSignatureError:
            self.logger.warning("JWT token expired")
            return None
        except jwt.InvalidTokenError as e:
            self.logger.warning(f"Invalid JWT token: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Authentication error: {e}")
            return None

    async def _get_user_from_supabase(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user data from Supabase."""
        try:
            # Get user organizations
            response = (
                self.supabase_client.client.table("user_organizations")
                .select("role, organizations (*)")
                .eq("user_id", user_id)
                .execute()
            )

            if not response.data:
                return None

            # Get primary organization (first one)
            org_data = response.data[0]
            organization = org_data["organizations"]

            return {
                "id": user_id,
                "role": org_data["role"],
                "organization_id": organization["id"],
                "organization_name": organization["name"],
                "organization_type": organization["type"],
                "subscription_tier": organization["subscription_tier"],
            }

        except Exception as e:
            self.logger.error(f"Failed to get user from Supabase: {e}")
            return None

    async def authorize_action(self, user: Dict[str, Any], resource: str, action: str) -> bool:
        """Authorize user action on resource."""
        try:
            user_role = user.get("role")
            user.get("organization_id")

            # Define role permissions
            role_permissions = {
                "owner": ["read", "write", "delete", "admin"],
                "admin": ["read", "write", "delete"],
                "operator": ["read", "write"],
                "viewer": ["read"],
            }

            # Check if user has required permission
            if action not in role_permissions.get(user_role, []):
                return False

            # Resource-specific authorization
            if resource == "organization":
                return user_role in ["owner", "admin"]
            elif resource == "vehicles":
                return user_role in ["owner", "admin", "operator"]
            elif resource == "charging_sessions":
                return user_role in ["owner", "admin", "operator", "viewer"]
            elif resource == "analytics":
                return user_role in ["owner", "admin", "operator", "viewer"]
            elif resource == "system_admin":
                return user_role == "owner"

            return True

        except Exception as e:
            self.logger.error(f"Authorization error: {e}")
            return False

    async def get_user_organizations(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all organizations for a user."""
        try:
            response = (
                self.supabase_client.client.table("user_organizations")
                .select("role, organizations (*)")
                .eq("user_id", user_id)
                .execute()
            )

            organizations = []
            for org_data in response.data:
                organization = org_data["organizations"]
                organizations.append(
                    {
                        "id": organization["id"],
                        "name": organization["name"],
                        "type": organization["type"],
                        "role": org_data["role"],
                        "subscription_tier": organization["subscription_tier"],
                    }
                )

            return organizations

        except Exception as e:
            self.logger.error(f"Failed to get user organizations: {e}")
            return []

    async def create_api_key(self, user_id: str, name: str, permissions: List[str]) -> str:
        """Create API key for service account."""
        try:
            # Generate API key
            {
                "user_id": user_id,
                "name": name,
                "permissions": json.dumps(permissions),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
            }

            # Store in database (you'd need to create an api_keys table)
            # For now, generate a JWT token
            payload = {
                "sub": user_id,
                "name": name,
                "permissions": permissions,
                "type": "api_key",
                "exp": datetime.now(timezone.utc) + timedelta(days=365),
            }

            api_key = jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)
            return api_key

        except Exception as e:
            self.logger.error(f"Failed to create API key: {e}")
            raise

    def generate_token(self, user_id: str, organization_id: str, role: str) -> str:
        """Generate JWT token for user."""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": user_id,
            "organization_id": organization_id,
            "role": role,
            "exp": now + self.token_expiry,
            "iat": now,
            "iss": self.jwt_issuer,
            "aud": self.jwt_audience,
        }

        return jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

    def verify_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """Verify API key and return user info."""
        try:
            payload = self._decode_jwt(api_key)

            if payload.get("type") != "api_key":
                return None

            return {
                "id": payload.get("sub"),
                "name": payload.get("name"),
                "permissions": payload.get("permissions", []),
                "type": "api_key",
            }

        except jwt.ExpiredSignatureError:
            self.logger.warning("API key expired")
            return None
        except jwt.InvalidTokenError as e:
            self.logger.warning(f"Invalid API key: {e}")
            return None
        except Exception as e:
            self.logger.error(f"API key verification error: {e}")
            return None

    def _decode_jwt(self, token: str) -> Dict[str, Any]:
        """Decode JWT using current and previous keys with issuer/audience checks."""
        secrets_to_try = [self.jwt_secret]
        if self.jwt_previous_secret:
            secrets_to_try.append(self.jwt_previous_secret)

        last_error: Optional[Exception] = None
        for secret in secrets_to_try:
            try:
                return jwt.decode(
                    token,
                    secret,
                    algorithms=[self.jwt_algorithm],
                    audience=self.jwt_audience,
                    issuer=self.jwt_issuer,
                    options={"verify_exp": True},
                )
            except jwt.InvalidTokenError as e:
                last_error = e
                continue

        if last_error:
            raise last_error
        raise jwt.InvalidTokenError("Token validation failed")

    def clear_user_cache(self, user_id: str) -> None:
        """Clear user cache."""
        cache_key = f"user:{user_id}"
        if cache_key in self.user_cache:
            del self.user_cache[cache_key]

    def clear_all_cache(self) -> None:
        """Clear all user cache."""
        self.user_cache.clear()


def require_auth(auth_manager: AuthManager):
    """Decorator to require authentication."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract token from request
            request = kwargs.get("request") or args[0] if args else None
            if not request:
                return {"error": "No request object"}, 401

            # Get token from headers
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                return {"error": "Missing or invalid authorization header"}, 401

            token = auth_header[7:]  # Remove 'Bearer ' prefix

            # Authenticate user
            user = await auth_manager.authenticate_user(token)
            if not user:
                return {"error": "Invalid or expired token"}, 401

            # Add user to kwargs
            kwargs["user"] = user

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_permission(resource: str, action: str):
    """Decorator to require specific permission."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user = kwargs.get("user")
            if not user:
                return {"error": "User not authenticated"}, 401

            # Get auth manager from the first argument (self)
            auth_manager = args[0].auth_manager if hasattr(args[0], "auth_manager") else None
            if not auth_manager:
                return {"error": "Auth manager not available"}, 500

            # Check authorization
            if not await auth_manager.authorize_action(user, resource, action):
                return {"error": "Insufficient permissions"}, 403

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_role(required_roles: List[str]):
    """Decorator to require specific role."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user = kwargs.get("user")
            if not user:
                return {"error": "User not authenticated"}, 401

            user_role = user.get("role")
            if user_role not in required_roles:
                return {"error": f'Required role: {", ".join(required_roles)}'}, 403

            return await func(*args, **kwargs)

        return wrapper

    return decorator


class RateLimiter:
    """Rate limiter for API endpoints."""

    def __init__(self):
        """Initialize rate limiter."""
        self.requests: Dict[str, List[datetime]] = {}
        self.cleanup_interval = timedelta(minutes=5)
        self.last_cleanup = datetime.now(timezone.utc)

    def is_allowed(self, key: str, limit: int, window: timedelta) -> bool:
        """Check if request is allowed."""
        now = datetime.now(timezone.utc)

        # Cleanup old entries periodically
        if now - self.last_cleanup > self.cleanup_interval:
            self._cleanup_old_entries(now)
            self.last_cleanup = now

        # Get request history for key
        if key not in self.requests:
            self.requests[key] = []

        request_times = self.requests[key]

        # Remove old requests outside window
        cutoff_time = now - window
        request_times[:] = [t for t in request_times if t > cutoff_time]

        # Check if under limit
        if len(request_times) >= limit:
            return False

        # Add current request
        request_times.append(now)
        return True

    def _cleanup_old_entries(self, now: datetime) -> None:
        """Clean up old rate limit entries."""
        cutoff_time = now - timedelta(hours=1)
        keys_to_remove = []

        for key, request_times in self.requests.items():
            request_times[:] = [t for t in request_times if t > cutoff_time]
            if not request_times:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self.requests[key]


def rate_limit(limiter: RateLimiter, limit: int, window_minutes: int = 60):
    """Decorator for rate limiting."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract user/organization ID for rate limiting
            user = kwargs.get("user", {})
            org_id = user.get("organization_id", "anonymous")

            # Check rate limit
            window = timedelta(minutes=window_minutes)
            if not limiter.is_allowed(f"org:{org_id}", limit, window):
                return {"error": "Rate limit exceeded"}, 429

            return await func(*args, **kwargs)

        return wrapper

    return decorator
