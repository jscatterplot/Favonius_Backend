"""Tests for Role-Based Access Control (RBAC).

Tests cover:
- Permission matrix correctness
- Role-based permission checks
- Unknown roles default to viewer
- require_permission dependency
- require_role dependency
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.security.rbac import (
    Permission,
    Role,
    ROLE_PERMISSIONS,
    has_permission,
    require_permission,
    require_role,
)


class TestPermissionMatrix:
    """Test the role-permission mapping."""

    def test_admin_has_all_permissions(self):
        """Admin role has every permission."""
        for perm in Permission:
            assert perm in ROLE_PERMISSIONS[Role.ADMIN]

    def test_operator_can_optimize(self):
        """Operator can trigger optimization."""
        assert Permission.OPTIMIZE_TRIGGER in ROLE_PERMISSIONS[Role.OPERATOR]
        assert Permission.OPTIMIZE_VIEW in ROLE_PERMISSIONS[Role.OPERATOR]

    def test_operator_cannot_admin(self):
        """Operator cannot access admin functions."""
        assert Permission.ADMIN_CONTROLLERS not in ROLE_PERMISSIONS[Role.OPERATOR]
        assert Permission.ADMIN_CONFIG not in ROLE_PERMISSIONS[Role.OPERATOR]

    def test_viewer_read_only(self):
        """Viewer has only read permissions."""
        viewer_perms = ROLE_PERMISSIONS[Role.VIEWER]
        assert Permission.OPTIMIZE_VIEW in viewer_perms
        assert Permission.DEPOT_VIEW in viewer_perms
        assert Permission.VEHICLE_VIEW in viewer_perms
        # No write permissions
        assert Permission.OPTIMIZE_TRIGGER not in viewer_perms
        assert Permission.VEHICLE_HANDOFF not in viewer_perms

    def test_auditor_can_view_audit(self):
        """Auditor can view and export audit data."""
        auditor_perms = ROLE_PERMISSIONS[Role.AUDITOR]
        assert Permission.AUDIT_VIEW in auditor_perms
        assert Permission.AUDIT_EXPORT in auditor_perms
        assert Permission.SECURITY_VIEW in auditor_perms

    def test_auditor_cannot_modify(self):
        """Auditor cannot modify anything."""
        auditor_perms = ROLE_PERMISSIONS[Role.AUDITOR]
        assert Permission.OPTIMIZE_TRIGGER not in auditor_perms
        assert Permission.DEPOT_MANAGE not in auditor_perms
        assert Permission.ADMIN_CONFIG not in auditor_perms


class TestHasPermission:
    """Test the has_permission function."""

    def test_admin_has_any_permission(self):
        """Admin has any permission."""
        assert has_permission("admin", Permission.OPTIMIZE_TRIGGER) is True
        assert has_permission("admin", Permission.ADMIN_CONFIG) is True

    def test_viewer_lacks_write(self):
        """Viewer lacks write permissions."""
        assert has_permission("viewer", Permission.OPTIMIZE_TRIGGER) is False

    def test_unknown_role_defaults_to_viewer(self):
        """Unknown role gets viewer permissions (least privilege)."""
        assert has_permission("unknown_role", Permission.DEPOT_VIEW) is True
        assert has_permission("unknown_role", Permission.OPTIMIZE_TRIGGER) is False

    def test_authenticated_defaults_to_viewer(self):
        """Supabase default 'authenticated' role defaults to viewer."""
        assert has_permission("authenticated", Permission.DEPOT_VIEW) is True
        assert has_permission("authenticated", Permission.ADMIN_CONFIG) is False


class TestRequirePermission:
    """Test the require_permission FastAPI dependency."""

    @pytest.mark.asyncio
    async def test_allows_with_permission(self):
        """Allows access when user has required permission."""
        token = {"sub": "user-1", "user_metadata": {"favonius_role": "admin"}}
        dep = require_permission(Permission.ADMIN_CONFIG)

        with patch("src.security.rbac.verify_token", return_value=token):
            # Should not raise
            await dep(token=token)

    @pytest.mark.asyncio
    async def test_denies_without_permission(self):
        """Denies access when user lacks required permission."""
        token = {"sub": "user-1", "user_metadata": {"favonius_role": "viewer"}}
        dep = require_permission(Permission.OPTIMIZE_TRIGGER)

        with pytest.raises(HTTPException) as exc_info:
            await dep(token=token)
        assert exc_info.value.status_code == 403


class TestRequireRole:
    """Test the require_role FastAPI dependency."""

    @pytest.mark.asyncio
    async def test_allows_matching_role(self):
        """Allows access when user has the required role."""
        token = {"sub": "user-1", "user_metadata": {"favonius_role": "admin"}}
        dep = require_role(Role.ADMIN)

        with patch("src.security.rbac.verify_token", return_value=token):
            await dep(token=token)

    @pytest.mark.asyncio
    async def test_denies_wrong_role(self):
        """Denies access when user has wrong role."""
        token = {"sub": "user-1", "user_metadata": {"favonius_role": "viewer"}}
        dep = require_role(Role.ADMIN)

        with pytest.raises(HTTPException) as exc_info:
            await dep(token=token)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_role_denied(self):
        """Unknown role is denied for admin-only resources."""
        token = {"sub": "user-1", "role": "custom_unknown"}
        dep = require_role(Role.ADMIN)

        with pytest.raises(HTTPException):
            await dep(token=token)
