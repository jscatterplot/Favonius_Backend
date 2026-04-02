"""JWT authentication for API endpoints with key rotation support.

Verifies Supabase-issued JWT tokens. Supports multiple valid signing
keys during rotation windows per NIS2 Article 21 requirements.

Environment variables:
    JWT_SECRET_KEY: Current Supabase JWT secret
    JWT_SECRET_KEY_PREVIOUS: Previous key (valid during rotation window)
    JWT_ALGORITHM: Signing algorithm (default HS256)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .secrets import get_secrets_manager

logger = logging.getLogger(__name__)

# Supabase uses HS256 by default
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

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
    for secret in secrets:
        try:
            payload = jwt.decode(
                token_str,
                secret,
                algorithms=[JWT_ALGORITHM],
                audience="authenticated",
            )
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

    # All keys failed
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Invalid token: {last_error}",
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
