"""JWT authentication for API endpoints with key rotation and JWKS support.

Verifies Supabase-issued JWT tokens. Supports two signing modes:

- HS256 — legacy symmetric signing. The current and previous secrets in
  ``JWT_SECRET_KEY`` / ``JWT_SECRET_KEY_PREVIOUS`` are both tried, so a
  Supabase JWT-secret rotation does not cause downtime (NIS2 Article 21).
- ES256 / RS256 / EdDSA — asymmetric signing introduced by Supabase's
  "JWT Signing Keys" feature. New Supabase projects (CLI ≥ 2.71.1) default
  to ES256. The verifier fetches the project's public keys from the JWKS
  endpoint and selects the right key by ``kid``.

The token's own ``alg`` header decides which path is taken, validated
against an explicit allowlist below — this prevents ``alg=none`` and any
algorithm Supabase would not have produced.

Environment variables:
    JWT_SECRET_KEY: Current Supabase HS256 secret (legacy projects).
    JWT_SECRET_KEY_PREVIOUS: Previous HS256 secret (valid during rotation).
    SUPABASE_URL: Project URL — used to derive the JWKS URL automatically.
    SUPABASE_JWKS_URL: Full JWKS URL override (optional).
    JWT_ISSUER: Expected ``iss`` claim (optional).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient, PyJWKClientError

from .secrets import get_secrets_manager

logger = logging.getLogger(__name__)

# Hardcoded algorithm allowlist. HS256 is the legacy Supabase default;
# ES256 is the new default for projects on JWT Signing Keys; RS256 / EdDSA
# are also offered as asymmetric options. Anything else (notably "none")
# is rejected before key lookup.
_HS_ALGORITHMS: tuple[str, ...] = ("HS256",)
_ASYMMETRIC_ALGORITHMS: tuple[str, ...] = ("ES256", "RS256", "EdDSA")
_ALLOWED_ALGORITHMS: tuple[str, ...] = _HS_ALGORITHMS + _ASYMMETRIC_ALGORITHMS

# Optional issuer validation (e.g. https://<ref>.supabase.co/auth/v1)
JWT_ISSUER: Optional[str] = os.getenv("JWT_ISSUER")

# JWKS client cache lifetime — default 1 hour; Supabase keys rotate rarely.
_JWKS_CACHE_LIFESPAN_S = int(os.getenv("JWT_JWKS_CACHE_LIFESPAN_S", "3600"))

security = HTTPBearer()

_jwks_client: Optional[PyJWKClient] = None


def _derive_jwks_url() -> Optional[str]:
    """Resolve the Supabase JWKS URL from env, preferring an explicit override."""
    explicit = os.getenv("SUPABASE_JWKS_URL")
    if explicit:
        return explicit.strip()
    base = os.getenv("SUPABASE_URL")
    if not base:
        return None
    return base.rstrip("/") + "/auth/v1/.well-known/jwks.json"


def _get_jwks_client() -> Optional[PyJWKClient]:
    """Return a memoised PyJWKClient if a JWKS URL is configured, else None."""
    global _jwks_client
    if _jwks_client is not None:
        return _jwks_client
    url = _derive_jwks_url()
    if not url:
        return None
    _jwks_client = PyJWKClient(url, cache_keys=True, lifespan=_JWKS_CACHE_LIFESPAN_S)
    return _jwks_client


def _reset_jwks_client_for_tests() -> None:
    """Clear the cached JWKS client. Test-only hook."""
    global _jwks_client
    _jwks_client = None


def _get_jwt_secrets() -> list[str]:
    """Return all valid HS256 secrets (current + optional previous).

    Raises:
        HTTPException(500): If no HS256 secret is configured but the caller
            needs one to verify a presented HS256 token.
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


def _decode_with_hs256(token_str: str, decode_kwargs: dict[str, Any]) -> dict:
    """Verify a HS256 token against current + previous secrets."""
    secrets = _get_jwt_secrets()
    last_error: Optional[Exception] = None
    for secret in secrets:
        try:
            return jwt.decode(token_str, secret, **decode_kwargs)
        except jwt.ExpiredSignatureError:
            raise
        except jwt.InvalidTokenError as e:
            last_error = e
            continue
    if last_error is not None:
        raise last_error
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token",
    )


def _decode_with_jwks(token_str: str, decode_kwargs: dict[str, Any]) -> dict:
    """Verify an asymmetric token using the project's JWKS public keys."""
    client = _get_jwks_client()
    if client is None:
        logger.error(
            "Received asymmetric JWT but no JWKS URL is configured. "
            "Set SUPABASE_URL or SUPABASE_JWKS_URL."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    try:
        signing_key = client.get_signing_key_from_jwt(token_str)
    except PyJWKClientError as e:
        logger.warning("JWKS key lookup failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return jwt.decode(token_str, signing_key.key, **decode_kwargs)


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Verify a Supabase JWT token and return the decoded payload.

    Picks the verification path from the token's ``alg`` header so the
    backend can simultaneously serve projects on legacy HS256 and projects
    on the new asymmetric signing keys. The algorithm is validated against
    a hardcoded allowlist before any key lookup.

    The returned payload contains:
      - sub: user UUID (auth.uid() in Supabase)
      - email: user email
      - role: "authenticated" (Supabase default)
      - aud: "authenticated"
      - exp: expiration timestamp
      - app_metadata: trusted Favonius tenancy claims (organization_id,
        favonius_role)
      - user_metadata: user-editable fields (e.g. is_demo) — not used for
        access control

    Raises HTTPException if the token is invalid or expired.
    """
    token_str = credentials.credentials

    try:
        header = jwt.get_unverified_header(token_str)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    alg = header.get("alg")
    if alg not in _ALLOWED_ALGORITHMS:
        logger.warning("Rejecting token with disallowed alg=%r", alg)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    decode_kwargs: dict[str, Any] = {
        "algorithms": [alg],
        "audience": "authenticated",
    }
    if JWT_ISSUER:
        decode_kwargs["issuer"] = JWT_ISSUER

    try:
        if alg in _HS_ALGORITHMS:
            return _decode_with_hs256(token_str, decode_kwargs)
        return _decode_with_jwks(token_str, decode_kwargs)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError:
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
                "SELECT EXISTS(SELECT 1 FROM sites "
                "WHERE id = $1::uuid AND organization_id = $2::uuid)",
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
