"""JWT authentication for API endpoints.

Verifies Supabase-issued JWT tokens. The JWT_SECRET_KEY environment
variable must be set to the Supabase project's JWT secret
(Dashboard → Settings → API → JWT Secret).

This ensures the frontend's Supabase session token is accepted
directly — no separate login endpoint needed on Service A.
"""

from __future__ import annotations

import os
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# The Supabase JWT secret — same secret that signs all Supabase access tokens.
# Set this env var to: Supabase Dashboard → Settings → API → JWT Secret
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

# Supabase uses HS256 by default
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

security = HTTPBearer()


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Verify a Supabase JWT token and return the decoded payload.

    The payload contains:
      - sub: user UUID (auth.uid() in Supabase)
      - email: user email
      - role: "authenticated" (Supabase default)
      - aud: "authenticated"
      - exp: expiration timestamp
      - user_metadata: custom fields (e.g. is_demo)

    Raises HTTPException if the token is invalid or expired.
    """
    if not JWT_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "JWT_SECRET_KEY not configured. "
                "Set it to your Supabase JWT secret "
                "(Dashboard → Settings → API → JWT Secret)."
            ),
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            audience="authenticated",  # Supabase sets aud="authenticated"
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
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


# Usage in FastAPI endpoints:
#
# @app.get("/depots/{depot_id}/state")
# async def get_state(depot_id: UUID, token: dict = Depends(verify_token)):
#     user_id = get_user_id(token)
#     ...
