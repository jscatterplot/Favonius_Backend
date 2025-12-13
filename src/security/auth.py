"""JWT authentication for API endpoints.

See PRD_v2.md Section 10.3 for authentication requirements.
"""
from __future__ import annotations

import os
import jwt
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# JWT configuration per PRD Section 10.3
JWT_ACCESS_TOKEN_EXPIRY = timedelta(hours=1)  # 1 hour access token
JWT_REFRESH_TOKEN_EXPIRY = timedelta(hours=24)  # 24 hour refresh token
JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY')  # Must be set in production
JWT_ALGORITHM = 'HS256'

security = HTTPBearer()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """Verify JWT token and return payload.
    
    Raises HTTPException if token is invalid or expired.
    
    Reference: PRD_v2.md Section 10.3
    """
    if not JWT_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret key not configured"
        )
    
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )


# Usage in FastAPI endpoints:
# @app.get("/depots/{depot_id}/state")
# async def get_state(depot_id: UUID, token: dict = Depends(verify_token)):
#     ...

