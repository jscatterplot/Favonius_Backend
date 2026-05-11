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

import asyncio
import logging
import os
from typing import Any, Optional

import asyncpg
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

# Email domains whose JWT subjects are auto-promoted to ``favonius_admin``.
# Comparison is on the part after the final ``@``, lowercased, exact match —
# so ``user@favoniusenergy.com`` matches but ``user@evil.favoniusenergy.com``
# and ``user@favoniusenergy.com.attacker.com`` do not.
_DEFAULT_FAVONIUS_ADMIN_DOMAINS: tuple[str, ...] = ("favoniusenergy.com",)


def _load_favonius_admin_domains() -> tuple[str, ...]:
    """Return the configured admin email domains (env override, lowercased)."""
    raw = os.getenv("FAVONIUS_ADMIN_EMAIL_DOMAINS")
    if not raw:
        return _DEFAULT_FAVONIUS_ADMIN_DOMAINS
    parsed = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return parsed or _DEFAULT_FAVONIUS_ADMIN_DOMAINS


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


def _try_get_hs256_secrets() -> Optional[list[str]]:
    """Return HS256 secrets if configured, else None."""
    try:
        return get_secrets_manager().get_rotation_secrets("JWT_SECRET_KEY")
    except ValueError:
        return None


def _get_jwt_secrets() -> list[str]:
    """Return all valid HS256 secrets (current + optional previous).

    Raises:
        HTTPException(500): If no HS256 secret is configured but the caller
            needs one (operational paths such as rate-limit key extraction).
    """
    secrets = _try_get_hs256_secrets()
    if secrets is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "JWT_SECRET_KEY not configured. "
                "Set it to your Supabase JWT secret "
                "(Dashboard → Settings → API → JWT Secret)."
            ),
        )
    return secrets


def _decode_with_hs256(token_str: str, decode_kwargs: dict[str, Any]) -> dict:
    """Verify a HS256 token against current + previous secrets."""
    secrets = _try_get_hs256_secrets()
    if secrets is None:
        logger.warning(
            "Received HS256 JWT but JWT_SECRET_KEY is not configured "
            "(asymmetric-only deployment)."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
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


def decode_jwt_for_rate_limit(token_str: str) -> Optional[dict[str, Any]]:
    """Verify JWT and return payload for per-user rate limiting.

    Mirrors ``verify_token`` verification paths (HS256 vs JWKS) so asymmetric
    deployments without ``JWT_SECRET_KEY`` still bucket by ``sub``. Returns
    ``None`` for unusable tokens; raises ``jwt.ExpiredSignatureError`` when the
    signature is valid but the token is expired (callers fall back to IP).
    """
    try:
        header = jwt.get_unverified_header(token_str)
    except jwt.InvalidTokenError:
        return None

    alg = header.get("alg")
    if alg not in _ALLOWED_ALGORITHMS:
        return None

    decode_kwargs: dict[str, Any] = {
        "algorithms": [str(alg)],
        "audience": "authenticated",
    }
    if JWT_ISSUER:
        decode_kwargs["issuer"] = JWT_ISSUER

    try:
        if alg in _HS_ALGORITHMS:
            return _decode_with_hs256(token_str, decode_kwargs)
        return _decode_with_jwks(token_str, decode_kwargs)
    except jwt.ExpiredSignatureError:
        raise
    except HTTPException:
        return None
    except jwt.PyJWTError:
        return None


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
      - email: user email — also used by :func:`get_user_role` to auto-promote
        a staff-domain address when the token shows a confirmed email identity
        (non-empty ``email_confirmed_at`` if present, else issuer-signed
        ``app_metadata`` / ``user_metadata`` rules in
        :func:`_jwt_email_confirmation_present`).
      - role: "authenticated" (Supabase default)
      - aud: "authenticated"
      - exp: expiration timestamp
      - app_metadata: trusted Favonius tenancy claims (organization_id,
        favonius_role)
      - user_metadata: user-editable fields (e.g. is_demo). Only
        ``email_verified: false`` is consulted for the staff-domain promotion
        gate; it does not grant extra privileges.

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
    except jwt.PyJWTError:
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
        # Static DB pool unavailable means the auth check cannot be evaluated.
        # Surface this as 503 so the client (and on-call) can distinguish it
        # from a real policy denial — silently returning 403 here previously
        # made every depot endpoint look access-denied during startup blips.
        logger.error(
            "depot access check unavailable: static DB pool not initialized " "(depot=%s org=%s)",
            depot_id,
            org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Depot access check is temporarily unavailable",
        )

    try:
        async with pool.acquire() as conn:
            has_access = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM sites "
                "WHERE id = $1::uuid AND organization_id = $2::uuid)",
                depot_id,
                org_id,
            )
    except (
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
        asyncpg.InternalClientError,
        OSError,
        asyncio.TimeoutError,
    ) as exc:
        # Real DB / network failure. Don't disguise as a policy denial — that
        # makes diagnosis impossible and falsely tells the user they lack
        # access. Log with traceback and surface 503 so the caller can retry.
        logger.error(
            "depot access check failed due to DB error for depot=%s org=%s: %s",
            depot_id,
            org_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Depot access check is temporarily unavailable",
        ) from exc

    if has_access:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: you do not have permission for this depot",
    )


def _app_metadata_indicates_confirmed_email_identity(meta: dict) -> bool:
    """True when issuer-signed metadata ties the session to email (not phone-only).

    ``app_metadata.provider`` / ``providers`` are set by Supabase Auth, not the
    end user. Phone-only confirmation must not satisfy staff-domain promotion
    when the email claim is otherwise unconstrained.
    """
    names: set[str] = set()
    prov = meta.get("provider")
    if isinstance(prov, str) and prov.strip():
        names.add(prov.strip().lower())
    provs = meta.get("providers")
    if isinstance(provs, list):
        for item in provs:
            if isinstance(item, str) and item.strip():
                names.add(item.strip().lower())
    if not names:
        return False
    # ``confirmed_at`` can reflect phone-only confirmation; reject phone-only.
    return not names <= {"phone"}


def _jwt_email_confirmation_present(token: dict) -> bool:
    """True if the JWT indicates a confirmed email identity for staff promotion.

    ``user_metadata`` is user-editable and must not *grant* confirmation, but
    Supabase mirrors ``email_verified: false`` there for unverified addresses;
    when explicitly ``False``, promotion is denied — including when a Custom
    Access Token Hook injects a non-empty ``email_confirmed_at``.

    Standard Supabase access tokens omit ``email_confirmed_at`` (that field
    lives on ``auth.users``); a hook may add it — when present and non-empty,
    it is honored only after the unverified mirror check above.

    Otherwise, rely on issuer-signed ``app_metadata`` (``provider`` /
    ``providers``): any identity beyond phone-only is treated as email-backed
    for this gate (OAuth and email magic-link sessions include non-phone
    providers). ``confirmed_at`` alone is not used: it is set when either email
    or phone is confirmed.
    """
    user_meta = token.get("user_metadata")
    if isinstance(user_meta, dict) and user_meta.get("email_verified") is False:
        return False

    val = token.get("email_confirmed_at")
    if isinstance(val, str) and val.strip():
        return True

    app_meta = token.get("app_metadata")
    if not isinstance(app_meta, dict):
        return False
    return _app_metadata_indicates_confirmed_email_identity(app_meta)


def _email_indicates_favonius_admin(token: dict) -> bool:
    """Return True if the JWT email belongs to a Favonius staff domain.

    Domain comparison is exact (no subdomain matching) and case-insensitive.
    Staff-domain auto-promotion requires a confirmed email identity per
    :func:`_jwt_email_confirmation_present` so unconfirmed signups cannot
    elevate by spoofing ``user_metadata``.
    """
    email = token.get("email")
    if not isinstance(email, str) or "@" not in email:
        return False

    if not _jwt_email_confirmation_present(token):
        return False

    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain in _load_favonius_admin_domains()


def get_user_role(token: dict) -> str:
    """Extract the Favonius role for the authenticated user.

    Resolution order:
      1. If the verified ``email`` claim belongs to a configured Favonius
         staff domain (default ``favoniusenergy.com``), the role is
         ``favonius_admin``. This grants platform-wide access to all depots
         and tenants and overrides any explicit ``app_metadata.favonius_role``
         so a stale Supabase metadata value cannot demote a Favonius
         employee. Configure additional or alternate domains via the
         ``FAVONIUS_ADMIN_EMAIL_DOMAINS`` env var (comma-separated).
      2. Otherwise ``app_metadata.favonius_role`` if present.
      3. Otherwise the Supabase top-level ``role`` claim
         (defaults to ``authenticated``).
    """
    if _email_indicates_favonius_admin(token):
        return "favonius_admin"
    meta = get_app_metadata(token)
    favonius_role = meta.get("favonius_role")
    if favonius_role:
        return str(favonius_role)
    return token.get("role", "authenticated")
