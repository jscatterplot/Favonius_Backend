"""JWT authentication for API endpoints with key rotation support.

Verifies Supabase-issued JWT tokens. Supports multiple valid signing
keys during rotation windows per NIS2 Article 21 requirements.

Environment variables:
    JWT_SECRET_KEY: Current Supabase JWT secret
    JWT_SECRET_KEY_PREVIOUS: Previous key (valid during rotation window)
    JWT_ALGORITHM: Signing algorithm (default HS256) — must be in allowlist
    JWT_ISSUER: Expected token issuer (optional, e.g. Supabase project URL)
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

# Optional issuer validation (set to your Supabase project URL)
JWT_ISSUER: Optional[str] = os.getenv("JWT_ISSUER")

security = HTTPBearer()


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
      - user_metadata: custom fields (e.g. is_demo)

    Raises HTTPException if the token is invalid or expired.
    """
    secrets = _get_jwt_secrets()
    token_str = credentials.credentials

    last_error: Optional[Exception] = None
    decode_kwargs: dict[str, Any] = {
        "algorithms": _ALLOWED_ALGORITHMS,
        "audience": "authenticated",
    }
    if JWT_ISSUER:
        decode_kwargs["issuer"] = JWT_ISSUER

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


async def verify_depot_access(depot_id: str, user: dict, pool: Any = None) -> None:
    """Verify the authenticated user is authorized to access a specific depot.

    Checks the JWT user_metadata.depot_ids claim first (fast path).
    Falls back to a DB query against user_depot_access if the claim is absent
    and a pool is provided. Raises 403 if access is denied.

    Args:
        depot_id: The depot being accessed.
        user: Decoded JWT payload from verify_token.
        pool: Optional asyncpg pool for DB-backed authorization.
    """
    # Fast path: check depot_ids claim in token
    metadata = user.get("user_metadata", {})
    depot_ids = metadata.get("depot_ids")
    if isinstance(depot_ids, list):
        if depot_id in depot_ids:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: you do not have permission for this depot",
        )

    # Check favonius_role — admins can access all depots
    favonius_role = metadata.get("favonius_role", "")
    if favonius_role == "admin":
        logger.warning(
            "Admin depot access bypass",
            extra={"user_id": user.get("sub"), "depot_id": depot_id},
        )
        return

    # Fallback: DB-backed authorization check
    if pool is not None:
        user_id = user.get("sub")
        if user_id:
            try:
                async with pool.acquire() as conn:
                    has_access = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM user_depot_access "
                        "WHERE user_id = $1 AND depot_id = $2::uuid)",
                        user_id,
                        depot_id,
                    )
                if has_access:
                    return
            except Exception:
                # Table may not exist yet — log and deny
                logger.warning(
                    "user_depot_access lookup failed for user=%s depot=%s",
                    user_id,
                    depot_id,
                )

    # No claim, not admin, no DB match → deny
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: you do not have permission for this depot",
    )


def get_user_depot_ids(token: dict) -> list[str]:
    """Return the list of depot UUIDs the user can access, from the JWT claim.

    Returns an empty list when the claim is absent (e.g., admin users who use
    the role-based bypass, or tokens issued before the Auth Hook was configured).
    """
    metadata = token.get("user_metadata", {})
    depot_ids = metadata.get("depot_ids")
    if isinstance(depot_ids, list):
        return [str(d) for d in depot_ids]
    return []


def get_user_role(token: dict) -> str:
    """Extract the user role from a verified token payload.

    Returns the Supabase role claim, or checks user_metadata for
    Favonius-specific role assignments.
    """
    # Check Favonius-specific role in user_metadata first
    metadata = token.get("user_metadata", {})
    favonius_role = metadata.get("favonius_role")
    if favonius_role:
        return favonius_role

    # Fall back to Supabase default role
    return token.get("role", "authenticated")
