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
_repair_cache: dict[str, float] = {}
_TTL_S = float(os.getenv("TENANT_MIRROR_TTL_S", "300"))


def clear_tenant_mirror_cache() -> None:
    """Clear in-process mirror cache (tests)."""
    _cache.clear()
    _repair_cache.clear()


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


# user_organizations.role lives in Supabase's static schema, which has a
# CHECK constraint allowing only Supabase vocab (owner|admin|operator|viewer).
# The backend's JWT carries Favonius vocab (customer_admin|customer_operator|
# favonius_admin). Writing the JWT vocab raises CheckViolationError, which
# silently fails best-effort mirrors and rolls back atomic-mirror depot
# creates. Translate at the write boundary; the backend never reads this
# column for authorization (it uses JWT.app_metadata.organization_id vs
# sites.organization_id), so the mapping only needs to satisfy the CHECK.
_FAVONIUS_TO_SUPABASE_ROLE: dict[str, str] = {
    "customer_admin": "owner",
    "customer_operator": "operator",
    "favonius_admin": "admin",  # mirror skips favonius_admin, kept for safety
}


def _supabase_role_for(favonius_role: str) -> str:
    """Translate Favonius role vocab to the Supabase user_organizations.role CHECK."""
    return _FAVONIUS_TO_SUPABASE_ROLE.get(favonius_role, "viewer")


# Reverse mapping for the self-heal path. Only roles that are unambiguous and
# safe to auto-derive are listed: 'admin' (favonius_admin) is intentionally
# excluded because backfilling staff promotion from a DB row would let a
# corrupted user_organizations entry escalate privilege; 'viewer' has no
# Favonius-vocab equivalent and is rarely the cause of a wizard-blocked signup.
_SUPABASE_TO_FAVONIUS_ROLE: dict[str, str] = {
    "owner": "customer_admin",
    "operator": "customer_operator",
}


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
        _supabase_role_for(role),
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


async def _fetch_single_user_org_membership(
    conn: "asyncpg.Connection", user_id: str
) -> Optional[tuple[str, str, Optional[str]]]:
    """Return ``(org_id, supabase_role, organization_name)`` if the user has
    exactly one ``user_organizations`` row whose role maps to a repairable
    Favonius role. Returns ``None`` for zero rows, multiple rows, or a role
    outside :data:`_SUPABASE_TO_FAVONIUS_ROLE` (admin / viewer / unknown).
    """
    rows = await conn.fetch(
        "SELECT uo.organization_id::text AS organization_id, uo.role, o.name "
        "FROM user_organizations uo "
        "LEFT JOIN organizations o ON o.id = uo.organization_id "
        "WHERE uo.user_id = $1::uuid",
        user_id,
    )
    if len(rows) != 1:
        return None
    row = rows[0]
    if row["role"] not in _SUPABASE_TO_FAVONIUS_ROLE:
        return None
    name = row["name"] if isinstance(row["name"], str) and row["name"].strip() else None
    return (str(row["organization_id"]), str(row["role"]), name)


async def _fetch_role_for_org(
    conn: "asyncpg.Connection", user_id: str, org_id: str
) -> Optional[tuple[str, Optional[str]]]:
    """Return ``(supabase_role, organization_name)`` for the specific (user, org) pair.

    Returns ``None`` when no ``user_organizations`` row exists for that pair, or when
    the role is not in :data:`_SUPABASE_TO_FAVONIUS_ROLE` (admin / viewer / unknown).
    Used by the Case B self-heal path where ``organization_id`` is present in the JWT
    but ``favonius_role`` is absent.
    """
    rows = await conn.fetch(
        "SELECT uo.role, o.name "
        "FROM user_organizations uo "
        "LEFT JOIN organizations o ON o.id = uo.organization_id "
        "WHERE uo.user_id = $1::uuid AND uo.organization_id = $2::uuid",
        user_id,
        org_id,
    )
    if not rows:
        return None
    row = rows[0]
    if row["role"] not in _SUPABASE_TO_FAVONIUS_ROLE:
        return None
    name = row["name"] if isinstance(row["name"], str) and row["name"].strip() else None
    return (str(row["role"]), name)


async def _patch_supabase_app_metadata(
    *,
    supabase_url: str,
    service_key: str,
    user_id: str,
    app_metadata: dict,
    timeout_s: float = 5.0,
) -> None:
    """PUT a user's ``app_metadata`` via the Supabase Auth admin API.

    Raises on non-2xx so the caller can decide whether to cache the repair or
    retry on the next request. Tests monkeypatch this helper to assert the
    payload shape without going through ``httpx``.
    """
    import httpx  # noqa: PLC0415 — keep import scoped to the repair path

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.put(
            f"{supabase_url}/auth/v1/admin/users/{user_id}",
            headers={
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
                "Content-Type": "application/json",
            },
            json={"app_metadata": app_metadata},
        )
        resp.raise_for_status()


async def repair_user_tenant_metadata(user: dict, pool: Optional["asyncpg.Pool"]) -> None:
    """Self-heal stale signups by writing tenancy claims back to Supabase.

    Handles two repair cases:

    **Case A** — ``organization_id`` is missing from the JWT: derive it from a
    single unambiguous ``user_organizations`` row and push the full triple
    (``favonius_role``, ``organization_id``, ``organization_name``) to Supabase.

    **Case B** — ``organization_id`` is present but ``favonius_role`` is absent
    (the Gustas/HRX pattern): look up the role for that specific org and push
    just ``favonius_role`` (and ``organization_name`` if also absent). The
    ``organization_id`` is already correct in the JWT and is not overwritten.

    The current request still proceeds with the unpatched token; the user's
    NEXT token refresh sees the corrected claims and the depot list becomes
    visible.

    No-op when:
      * both ``organization_id`` and ``favonius_role`` are already set;
      * ``sub`` is missing or the role is ``favonius_admin``;
      * Case A: zero or multiple ``user_organizations`` rows (ambiguous);
      * Case B: no ``user_organizations`` row for the token's org, or the role
        maps to an unrepresentable value (admin / viewer);
      * ``SUPABASE_URL`` or ``SUPABASE_SERVICE_KEY`` are unset (dev/local);
      * ``pool`` is ``None`` or the repair was already issued within the TTL.

    Errors are logged and swallowed so auth never breaks on a bad signup.
    """
    user_id = user.get("sub")
    if not user_id:
        return
    if get_user_role(user) == "favonius_admin":
        return
    if pool is None:
        return

    org_id = get_user_organization_id(user)
    app_meta = user.get("app_metadata") if isinstance(user.get("app_metadata"), dict) else {}
    has_favonius_role = bool(app_meta.get("favonius_role"))
    if org_id is not None and has_favonius_role:
        return  # nothing to repair

    key = str(user_id)
    now = time.monotonic()
    cached_until = _repair_cache.get(key)
    if cached_until is not None and cached_until > now:
        return

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not service_key:
        return

    if org_id is None:
        # Case A: organization_id missing — derive from a single membership row.
        try:
            async with pool.acquire() as conn:
                membership = await _fetch_single_user_org_membership(conn, user_id)
        except Exception:
            logger.warning(
                "tenant_metadata_repair: db lookup failed user=%s",
                user_id,
                exc_info=True,
            )
            return

        if membership is None:
            # Cache the negative so we don't re-scan on every request.
            _repair_cache[key] = now + _TTL_S
            return

        org_id_derived, supabase_role, org_name = membership
        favonius_role = _SUPABASE_TO_FAVONIUS_ROLE[supabase_role]
        payload: dict = {
            "favonius_role": favonius_role,
            "organization_id": org_id_derived,
            "organization_name": org_name or f"org-{org_id_derived[:8]}",
        }
        log_org_id = org_id_derived

    else:
        # Case B: organization_id present but favonius_role missing.
        try:
            async with pool.acquire() as conn:
                role_result = await _fetch_role_for_org(conn, user_id, org_id)
        except Exception:
            logger.warning(
                "tenant_metadata_repair: db lookup failed user=%s",
                user_id,
                exc_info=True,
            )
            return

        if role_result is None:
            _repair_cache[key] = now + _TTL_S
            return

        supabase_role, org_name = role_result
        favonius_role = _SUPABASE_TO_FAVONIUS_ROLE[supabase_role]
        payload = {"favonius_role": favonius_role}
        if not app_meta.get("organization_name") and org_name:
            payload["organization_name"] = org_name
        log_org_id = org_id

    try:
        await _patch_supabase_app_metadata(
            supabase_url=supabase_url,
            service_key=service_key,
            user_id=user_id,
            app_metadata=payload,
        )
    except Exception:
        logger.warning(
            "tenant_metadata_repair: admin API call failed user=%s org=%s",
            user_id,
            log_org_id,
            exc_info=True,
        )
        # Do NOT cache on failure: the next request should retry so a transient
        # outage doesn't lock the user out of the wizard for a full TTL.
        return

    # Patch the in-flight user dict so the current request's auth check sees
    # the corrected role without requiring a token refresh. Safe because:
    # (a) we only reach here when favonius_role was absent — we never overwrite
    #     an existing lower-privilege claim; (b) the payload is the same data
    #     we just pushed to Supabase as the authoritative source.
    if isinstance(user.get("app_metadata"), dict):
        user["app_metadata"].update(payload)
    else:
        user["app_metadata"] = dict(payload)

    logger.warning(
        "tenant_metadata_repair: backfilled app_metadata user=%s org=%s role=%s",
        user_id,
        log_org_id,
        favonius_role,
    )
    _repair_cache[key] = now + _TTL_S


async def ensure_tenant_mirrored(user: dict = Depends(verify_token)) -> dict:
    """FastAPI dependency: verify JWT, repair stale signups, mirror tenancy."""
    from ..api import main as api_main  # noqa: PLC0415 — avoid import cycle at startup

    pool = api_main.db_pools.static if api_main.db_pools else None
    try:
        # Repair patches Supabase AND the in-flight user dict when favonius_role
        # was absent, so the current request's auth check and the subsequent
        # mirror call both see the corrected claims.
        await repair_user_tenant_metadata(user, pool)
    except Exception:
        logger.warning("ensure_tenant_mirrored: repair step failed", exc_info=True)
    try:
        await mirror_user_tenant(user, pool)
    except Exception:
        # Defense in depth: ``mirror_user_tenant`` already swallows DB errors; this
        # catches tests/patches and any unexpected failure so auth never breaks.
        logger.warning("ensure_tenant_mirrored: mirror step failed", exc_info=True)
    return user
