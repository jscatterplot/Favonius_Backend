"""JWT authentication for API endpoints with key rotation support.

Verifies Supabase-issued JWT tokens. Supports multiple valid signing
keys during rotation windows per NIS2 Article 21 requirements.

Environment variables:
    JWT_SECRET_KEY: Current Supabase JWT secret
    JWT_SECRET_KEY_PREVIOUS: Previous key (valid during rotation window)
    JWT_ALGORITHM: Signing algorithm (default HS256) — must be in allowlist
    JWT_ISSUER: Expected token issuer. If unset and ``SUPABASE_URL`` is
        configured, it is auto-derived as ``f"{SUPABASE_URL}/auth/v1"``
        (the issuer Supabase Auth emits by default). Set explicitly to
        override (e.g. for non-Supabase IDPs).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .secrets import get_secrets_manager

logger = logging.getLogger(__name__)

# Security: hardcoded algorithm allowlist — never trust env alone
_ALLOWED_ALGORITHMS = ["HS256"]
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
if JWT_ALGORITHM not in _ALLOWED_ALGORITHMS:
    raise RuntimeError(
        f"JWT_ALGORITHM '{JWT_ALGORITHM}' is not in the allowlist {_ALLOWED_ALGORITHMS}. "
        "Supabase uses HS256. Do NOT set this to 'none' or 'RS256' unless you update "
        "the allowlist and verification key type accordingly."
    )

security = HTTPBearer()


def _get_jwt_issuer() -> Optional[str]:
    """Resolve the expected JWT issuer.

    Priority:
      1. ``JWT_ISSUER`` env var (explicit override).
      2. ``SUPABASE_URL`` env var → derive ``f"{url}/auth/v1"``
         (the ``iss`` claim Supabase Auth/GoTrue emits).
      3. ``None`` — issuer check skipped.
    """
    explicit = os.getenv("JWT_ISSUER")
    if explicit:
        return explicit
    supabase_url = os.getenv("SUPABASE_URL")
    if supabase_url:
        return f"{supabase_url.rstrip('/')}/auth/v1"
    return None


def _get_jwt_secrets() -> list[str]:
    """Get all valid JWT secrets (current + previous during rotation).

    Returns:
        List of valid JWT secret strings.

    Raises:
        HTTPException: If no JWT secrets are configured.
    """
    try:
        return get_secrets_manager().get_rotation_secrets("JWT_SECRET_KEY")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "JWT_SECRET_KEY not configured. "
                "Set it to your Supabase JWT secret "
                "(Dashboard → Settings → API → JWT Secret)."
            ),
        )


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Verify a Supabase JWT token and return the decoded payload.

    Tries all valid signing keys during rotation windows. The current
    key is tried first, then the previous key if rotation is in progress.

    The payload contains:
      - sub: user UUID (auth.uid() in Supabase)
      - email: user email
      - role: "authenticated" (Supabase default)
      - aud: "authenticated"
      - exp: expiration timestamp
      - app_metadata: Favonius tenancy (organization_id, favonius_role) — trusted claims
      - user_metadata: user-editable fields (e.g. is_demo) — not used for access control

    Raises HTTPException if the token is invalid or expired.
    """
    secrets = _get_jwt_secrets()
    token_str = credentials.credentials

    last_error: Optional[Exception] = None
    decode_kwargs: dict[str, Any] = {
        "algorithms": _ALLOWED_ALGORITHMS,
        "audience": "authenticated",
    }
    issuer = _get_jwt_issuer()
    if issuer:
        decode_kwargs["issuer"] = issuer

    for secret in secrets:
        try:
            payload = jwt.decode(token_str, secret, **decode_kwargs)
            return payload
        except jwt.ExpiredSignatureError:
            # Expired tokens fail regardless of which key was used
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
            )
        except jwt.InvalidTokenError as e:
            last_error = e
            continue  # Try next key during rotation

    # All keys failed — redact token details from the error
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token",
    )


def get_user_id(token: dict) -> str:
    """Extract the Supabase user UUID from a verified token payload."""
    user_id = token.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' claim",
        )
    return user_id


def get_user_email(token: dict) -> Optional[str]:
    """Extract the user email from a verified token payload."""
    return token.get("email")


def is_demo_user(token: dict) -> bool:
    """Check if the authenticated user is a demo user."""
    metadata = token.get("user_metadata", {})
    return metadata.get("is_demo", False) is True


def get_app_metadata(token: dict) -> dict:
    """Return Supabase app_metadata dict (service-controlled), or empty dict."""
    meta = token.get("app_metadata")
    return meta if isinstance(meta, dict) else {}


def get_user_organization_id(token: dict) -> Optional[str]:
    """Return organization UUID string from app_metadata.organization_id, if set."""
    org = get_app_metadata(token).get("organization_id")
    if org is None or org == "":
        return None
    return str(org)


def is_platform_admin(token: dict) -> bool:
    """True if JWT carries Favonius platform admin (all tenants)."""
    return get_user_role(token) == "favonius_admin"


async def verify_depot_access(depot_id: str, user: dict, pool: Any = None) -> None:
    """Verify the authenticated user may access the depot (tenant + DB check).

    - ``favonius_admin`` bypasses tenant checks.
    - ``customer_admin`` / ``customer_operator`` require ``app_metadata.organization_id``
      and a matching ``depots.organization_id`` row (via static DB pool).

    ``user_metadata`` is not used for authorization.

    Args:
        depot_id: The depot being accessed.
        user: Decoded JWT payload from verify_token.
        pool: asyncpg pool for static (Supabase) reference data.
    """
    if is_platform_admin(user):
        return

    role = get_user_role(user)
    if role not in ("customer_admin", "customer_operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: you do not have permission for this depot",
        )

    org_id = get_user_organization_id(user)
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: missing organization_id in token app_metadata",
        )

    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: you do not have permission for this depot",
        )

    try:
        async with pool.acquire() as conn:
            has_access = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM depots "
                "WHERE depot_id = $1::uuid AND organization_id = $2::uuid)",
                depot_id,
                org_id,
            )
        if has_access:
            return
    except Exception:
        logger.warning(
            "depot organization access check failed for depot=%s org=%s",
            depot_id,
            org_id,
            exc_info=True,
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: you do not have permission for this depot",
    )


def get_user_role(token: dict) -> str:
    """Extract Favonius role from ``app_metadata.favonius_role``, else Supabase ``role``."""
    meta = get_app_metadata(token)
    favonius_role = meta.get("favonius_role")
    if favonius_role:
        return str(favonius_role)
    return token.get("role", "authenticated")
