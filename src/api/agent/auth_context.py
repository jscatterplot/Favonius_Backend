"""Server-side AuthContext for the depot chat agent.

Built once per turn from a verified Supabase JWT payload plus a single
Supabase query. Mirrors the visible-depots pattern at
``src/api/main.py:2705`` (``GET /me/depots``).

The agent never calls :func:`src.security.auth.verify_depot_access` per
depot. Scoping is enforced once here, by computing
``visible_depot_ids`` from ``sites.organization_id``. Every downstream
resolver and SQL compiler then filters on that list — a user cannot
reference a depot they cannot see, because its UUID is never in the
candidate set.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, cast, get_args
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

from src.db.queries import get_all_depots, get_depots_for_organization
from src.security.auth import get_user_id, get_user_organization_id, get_user_role

AgentRole = Literal["favonius_admin", "customer_admin", "customer_operator"]

_ALLOWED_ROLES: tuple[str, ...] = get_args(AgentRole)


class AuthContext(BaseModel):
    """The auth context the agent uses for the lifetime of one turn."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    organization_id: Optional[UUID]
    role: AgentRole
    visible_depot_ids: list[UUID]


async def build_auth_context(token_payload: dict, static_pool: Any) -> AuthContext:
    """Build an :class:`AuthContext` from a verified JWT payload.

    For ``favonius_admin``, ``visible_depot_ids`` covers every depot in the
    system. For ``customer_admin`` / ``customer_operator``, it covers only
    the caller's organization. A non-admin caller without an
    ``organization_id`` claim is rejected with 403 — the agent has no
    safe scope it can enforce against such a token.

    Args:
        token_payload: Decoded JWT payload from
            :func:`src.security.auth.verify_token`.
        static_pool: asyncpg pool (or connection) for the static Supabase
            schema where the depot tables live.

    Returns:
        An :class:`AuthContext` whose ``visible_depot_ids`` may be empty
        if the caller's org has no depots — that is not an error here;
        the resolver will surface it as "not found" downstream.

    Raises:
        HTTPException(401): If the token is missing the ``sub`` claim.
        HTTPException(403): If the role is not one of the agent-permitted
            values, or if a non-admin caller has no ``organization_id``.
    """
    user_id = UUID(get_user_id(token_payload))

    # ``get_user_role`` is the favonius-aware role getter (returns
    # ``app_metadata.favonius_role`` if set, else the Supabase top-level
    # ``role``). The architecture doc references this as
    # ``get_user_favonius_role``; the existing function name is kept here
    # to avoid inventing an alias.
    role = get_user_role(token_payload)
    if role not in _ALLOWED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{role}' is not permitted to use the agent",
        )

    org_id_str = get_user_organization_id(token_payload)

    if role == "favonius_admin":
        depots = await get_all_depots(static_pool)
        organization_id = UUID(org_id_str) if org_id_str else None
    else:
        if org_id_str is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="user has no organization",
            )
        depots = await get_depots_for_organization(static_pool, org_id_str)
        organization_id = UUID(org_id_str)

    visible_depot_ids = [UUID(str(row["depot_id"])) for row in depots]

    return AuthContext(
        user_id=user_id,
        organization_id=organization_id,
        role=cast(AgentRole, role),
        visible_depot_ids=visible_depot_ids,
    )
