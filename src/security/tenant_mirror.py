"""Just-in-time mirror of Supabase JWT tenancy into static DB rows.

``organizations`` / ``user_organizations`` are write-side caches for FKs and
joins. Authorization still uses ``app_metadata`` vs ``sites.organization_id``;
this module never trusts ``user_metadata``.

Supabase owns the canonical naming (``organizations.id``, ``user_organizations``).
This module aliases ``id`` to ``organization_id`` only at the SQL boundary.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, NamedTuple, Optional

from fastapi import Depends

from .auth import get_user_organization_id, get_user_role, verify_token

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


class _TenantCacheEntry(NamedTuple):
    org_id: Optional[str]
    role: str
    expires_at: float


_cache: dict[str, _TenantCacheEntry] = {}
_TTL_S = float(os.getenv("TENANT_MIRROR_TTL_S", "300"))


def clear_tenant_mirror_cache() -> None:
    """Clear in-process mirror cache (tests)."""
    _cache.clear()


def _extract_mirror_inputs(user: dict) -> tuple[Optional[str], Optional[str], str, Optional[str]]:
    """Pull (user_id, org_id, role, organization_name) from a verified JWT."""
    user_id = user.get("sub")
    org_id = get_user_organization_id(user)
    role = get_user_role(user)
    app_metadata = user.get("app_metadata") if isinstance(user.get("app_metadata"), dict) else {}
    organization_name = app_metadata.get("organization_name")
    if not isinstance(organization_name, str) or not organization_name.strip():
        organization_name = f"org-{org_id[:8]}" if org_id else None
    return user_id, org_id, role, organization_name


async def _execute_tenant_mirror_upserts(
    conn: "asyncpg.Connection",
    *,
    user_id: str,
    org_id: str,
    role: str,
    organization_name: Optional[str],
) -> None:
    """Run the org + membership UPSERTs on an existing connection. Errors propagate."""
    await conn.execute(
        "INSERT INTO organizations (id, name) " "VALUES ($1::uuid, $2) ON CONFLICT (id) DO NOTHING",
        org_id,
        organization_name,
    )
    # Supabase models user_organizations as multi-org-per-user with PK
    # (user_id, organization_id). The backend authorizes per-request via
    # the JWT's app_metadata.organization_id, so writing a row per
    # (user, org) pair is correct: a user with multiple Supabase orgs
    # gets one row each, and the role on the (user, jwt-org) pair is
    # what gets refreshed.
    await conn.execute(
        "INSERT INTO user_organizations (user_id, organization_id, role) "
        "VALUES ($1::uuid, $2::uuid, $3) "
        "ON CONFLICT (user_id, organization_id) DO UPDATE "
        "SET role = EXCLUDED.role",
        user_id,
        org_id,
        role,
    )


async def mirror_user_tenant(user: dict, pool: Optional["asyncpg.Pool"]) -> None:
    """Best-effort UPSERT of org + membership from verified JWT claims.

    No-op for ``favonius_admin``, users without ``organization_id``, or when
    ``pool`` is None. DB errors are logged and swallowed. Use
    :func:`mirror_user_tenant_atomic` from inside an existing transaction when
    the caller needs the membership row to be a hard prerequisite.

    Args:
        user: Decoded JWT from ``verify_token``.
        pool: Static (reference) asyncpg pool, or None.
    """
    user_id, org_id, role, organization_name = _extract_mirror_inputs(user)
    if not user_id or not org_id or role == "favonius_admin":
        return

    key = str(user_id)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached.expires_at > now and cached.org_id == org_id and cached.role == role:
        return

    if pool is None:
        return

    try:
        async with pool.acquire() as conn, conn.transaction():
            await _execute_tenant_mirror_upserts(
                conn,
                user_id=user_id,
                org_id=org_id,
                role=role,
                organization_name=organization_name,
            )
        _cache[key] = _TenantCacheEntry(org_id, role, now + _TTL_S)
    except Exception:
        logger.warning(
            "tenant mirror failed user=%s org=%s",
            user_id,
            org_id,
            exc_info=True,
        )


async def mirror_user_tenant_atomic(conn: "asyncpg.Connection", user: dict) -> None:
    """Run the org + membership UPSERTs on ``conn``. Errors propagate so the
    enclosing transaction can roll back.

    Use this from endpoints (e.g. depot creation) where the membership row is
    a hard prerequisite for downstream RLS-gated reads. Does not update the
    in-process cache: the caller owns the transaction; caching before commit
    would skip re-UPSERTs after rollback until TTL expiry.

    No-op for ``favonius_admin`` or users without ``organization_id``.
    """
    user_id, org_id, role, organization_name = _extract_mirror_inputs(user)
    if not user_id or not org_id or role == "favonius_admin":
        return

    await _execute_tenant_mirror_upserts(
        conn,
        user_id=user_id,
        org_id=org_id,
        role=role,
        organization_name=organization_name,
    )


async def ensure_tenant_mirrored(user: dict = Depends(verify_token)) -> dict:
    """FastAPI dependency: verify JWT then mirror org/membership (best-effort)."""
    from ..api import main as api_main  # noqa: PLC0415 — avoid import cycle at startup

    pool = api_main.db_pools.static if api_main.db_pools else None
    try:
        await mirror_user_tenant(user, pool)
    except Exception:
        # Defense in depth: ``mirror_user_tenant`` already swallows DB errors; this
        # catches tests/patches and any unexpected failure so auth never breaks.
        logger.warning("ensure_tenant_mirrored: mirror step failed", exc_info=True)
    return user
