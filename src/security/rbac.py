"""Role-Based Access Control (RBAC) for NIS2 compliance.

NIS2 Article 21 requires access control policies. This module provides:
- Role definitions (admin, operator, viewer, auditor)
- Permission matrix mapping roles to allowed actions
- FastAPI dependency for endpoint-level authorization
- Integration with Supabase JWT user_metadata.favonius_role

Roles are stored in Supabase user_metadata and verified at the API layer.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from fastapi import Depends, HTTPException, status

from .auth import get_user_role, verify_token

logger = logging.getLogger(__name__)


class Role(str, Enum):
    """User roles for Favonius Energy platform.

    Roles follow the principle of least privilege per NIS2 Article 21.
    """

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    AUDITOR = "auditor"


class Permission(str, Enum):
    """Granular permissions mapped to API operations."""

    # Optimization
    OPTIMIZE_TRIGGER = "optimize:trigger"
    OPTIMIZE_VIEW = "optimize:view"

    # Depot management
    DEPOT_VIEW = "depot:view"
    DEPOT_MANAGE = "depot:manage"

    # Vehicle operations
    VEHICLE_VIEW = "vehicle:view"
    VEHICLE_HANDOFF = "vehicle:handoff"

    # System administration
    ADMIN_CONTROLLERS = "admin:controllers"
    ADMIN_CONFIG = "admin:config"

    # Audit and compliance
    AUDIT_VIEW = "audit:view"
    AUDIT_EXPORT = "audit:export"

    # Security
    SECURITY_VIEW = "security:view"


# ── Permission Matrix ────────────────────────────────────────────────────

ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: {p for p in Permission},  # Admin has all permissions
    Role.OPERATOR: {
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
    Role.AUDITOR: {
        Permission.OPTIMIZE_VIEW,
        Permission.DEPOT_VIEW,
        Permission.VEHICLE_VIEW,
        Permission.AUDIT_VIEW,
        Permission.AUDIT_EXPORT,
        Permission.SECURITY_VIEW,
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
        # Unknown role — default to viewer (least privilege)
        logger.warning("Unknown role '%s', defaulting to viewer permissions", role)
        role_enum = Role.VIEWER

    return permission in ROLE_PERMISSIONS.get(role_enum, set())


def require_permission(permission: Permission):
    """FastAPI dependency that checks for a specific permission.

    Usage:
        @app.post("/optimize")
        async def optimize(
            token: dict = Depends(verify_token),
            _auth: None = Depends(require_permission(Permission.OPTIMIZE_TRIGGER)),
        ):
            ...
    """

    async def _check(token: dict = Depends(verify_token)) -> None:
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
    """FastAPI dependency that checks for a minimum role level.

    Usage:
        @app.get("/admin/controllers")
        async def list_controllers(
            token: dict = Depends(verify_token),
            _auth: None = Depends(require_role(Role.ADMIN)),
        ):
            ...
    """

    async def _check(token: dict = Depends(verify_token)) -> None:
        role = get_user_role(token)
        try:
            user_role = Role(role)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Unknown role: {role}",
            )

        if user_role != required_role and required_role == Role.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Admin access required. Current role: {role}",
            )

    return _check
