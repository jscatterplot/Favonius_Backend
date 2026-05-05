"""Role-Based Access Control (RBAC) for NIS2 compliance.

NIS2 Article 21 requires access control policies. This module provides:
- Role definitions (favonius_admin, customer_admin, customer_operator)
- Permission matrix mapping roles to allowed actions
- FastAPI dependency for endpoint-level authorization
- Integration with Supabase JWT app_metadata.favonius_role

Roles are stored in Supabase app_metadata and verified at the API layer.
"""

from __future__ import annotations

import logging
from enum import Enum

from fastapi import Depends, HTTPException, status

from .auth import get_user_role
from .tenant_mirror import ensure_tenant_mirrored

logger = logging.getLogger(__name__)


class Role(str, Enum):
    """User roles for Favonius Energy platform."""

    FAVONIUS_ADMIN = "favonius_admin"
    CUSTOMER_ADMIN = "customer_admin"
    CUSTOMER_OPERATOR = "customer_operator"
    # Used only as least-privilege fallback when role string is unknown
    VIEWER = "viewer"


class Permission(str, Enum):
    """Granular permissions mapped to API operations."""

    OPTIMIZE_TRIGGER = "optimize:trigger"
    OPTIMIZE_VIEW = "optimize:view"
    DEPOT_VIEW = "depot:view"
    DEPOT_MANAGE = "depot:manage"
    VEHICLE_VIEW = "vehicle:view"
    VEHICLE_HANDOFF = "vehicle:handoff"
    ADMIN_CONTROLLERS = "admin:controllers"
    ADMIN_CONFIG = "admin:config"
    AUDIT_VIEW = "audit:view"
    AUDIT_EXPORT = "audit:export"
    SECURITY_VIEW = "security:view"


ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.FAVONIUS_ADMIN: {p for p in Permission},
    Role.CUSTOMER_ADMIN: {
        Permission.OPTIMIZE_TRIGGER,
        Permission.OPTIMIZE_VIEW,
        Permission.DEPOT_VIEW,
        Permission.DEPOT_MANAGE,
        Permission.VEHICLE_VIEW,
        Permission.VEHICLE_HANDOFF,
        Permission.ADMIN_CONFIG,
    },
    Role.CUSTOMER_OPERATOR: {
        Permission.OPTIMIZE_TRIGGER,
        Permission.OPTIMIZE_VIEW,
        Permission.DEPOT_VIEW,
        Permission.DEPOT_MANAGE,
        Permission.VEHICLE_VIEW,
        Permission.VEHICLE_HANDOFF,
    },
    Role.VIEWER: {
        Permission.OPTIMIZE_VIEW,
        Permission.DEPOT_VIEW,
        Permission.VEHICLE_VIEW,
    },
}


def has_permission(role: str, permission: Permission) -> bool:
    """Check if a role has a specific permission.

    Args:
        role: Role string (from JWT token).
        permission: Required permission.

    Returns:
        True if the role has the permission.
    """
    try:
        role_enum = Role(role)
    except ValueError:
        logger.warning("Unknown role '%s', defaulting to viewer permissions", role)
        role_enum = Role.VIEWER

    return permission in ROLE_PERMISSIONS.get(role_enum, set())


def require_permission(permission: Permission):
    """FastAPI dependency that checks for a specific permission."""

    async def _check(token: dict = Depends(ensure_tenant_mirrored)) -> None:
        role = get_user_role(token)
        if not has_permission(role, permission):
            logger.warning(
                "Access denied: user %s (role=%s) lacks permission %s",
                token.get("sub", "unknown"),
                role,
                permission.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required: {permission.value}",
            )

    return _check


def require_role(required_role: Role):
    """FastAPI dependency that requires an exact role match."""

    async def _check(token: dict = Depends(ensure_tenant_mirrored)) -> None:
        role = get_user_role(token)
        try:
            user_role = Role(role)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Unknown role: {role}",
            )

        if user_role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role: {required_role.value}. Current role: {role}",
            )

    return _check


def require_favonius_admin():
    """Require Supabase app_metadata.favonius_role == favonius_admin."""

    async def _check(token: dict = Depends(ensure_tenant_mirrored)) -> None:
        role = get_user_role(token)
        if role != Role.FAVONIUS_ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Favonius platform administrator access required",
            )

    return _check
