"""Tests for Role-Based Access Control (RBAC).

Tests cover:
- Permission matrix correctness
- Role-based permission checks
- Unknown roles default to viewer
- require_permission dependency
- require_role dependency
"""

from __future__ import annotations

from unittest.mock import patch

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

    def test_favonius_admin_has_all_permissions(self):
        for perm in Permission:
            assert perm in ROLE_PERMISSIONS[Role.FAVONIUS_ADMIN]

    def test_customer_operator_can_optimize(self):
        assert Permission.OPTIMIZE_TRIGGER in ROLE_PERMISSIONS[Role.CUSTOMER_OPERATOR]
        assert Permission.OPTIMIZE_VIEW in ROLE_PERMISSIONS[Role.CUSTOMER_OPERATOR]

    def test_customer_operator_cannot_admin_config(self):
        assert Permission.ADMIN_CONTROLLERS not in ROLE_PERMISSIONS[Role.CUSTOMER_OPERATOR]
        assert Permission.ADMIN_CONFIG not in ROLE_PERMISSIONS[Role.CUSTOMER_OPERATOR]

    def test_customer_admin_can_config(self):
        assert Permission.ADMIN_CONFIG in ROLE_PERMISSIONS[Role.CUSTOMER_ADMIN]
        assert Permission.ADMIN_CONTROLLERS not in ROLE_PERMISSIONS[Role.CUSTOMER_ADMIN]

    def test_viewer_read_only(self):
        viewer_perms = ROLE_PERMISSIONS[Role.VIEWER]
        assert Permission.OPTIMIZE_VIEW in viewer_perms
        assert Permission.DEPOT_VIEW in viewer_perms
        assert Permission.VEHICLE_VIEW in viewer_perms
        assert Permission.OPTIMIZE_TRIGGER not in viewer_perms
        assert Permission.VEHICLE_HANDOFF not in viewer_perms


class TestHasPermission:
    """Test the has_permission function."""

    def test_favonius_admin_has_any_permission(self):
        assert has_permission("favonius_admin", Permission.OPTIMIZE_TRIGGER) is True
        assert has_permission("favonius_admin", Permission.ADMIN_CONFIG) is True

    def test_viewer_lacks_write(self):
        assert has_permission("viewer", Permission.OPTIMIZE_TRIGGER) is False

    def test_unknown_role_defaults_to_viewer(self):
        assert has_permission("unknown_role", Permission.DEPOT_VIEW) is True
        assert has_permission("unknown_role", Permission.OPTIMIZE_TRIGGER) is False

    def test_authenticated_defaults_to_viewer(self):
        assert has_permission("authenticated", Permission.DEPOT_VIEW) is True
        assert has_permission("authenticated", Permission.ADMIN_CONFIG) is False


class TestRequirePermission:
    """Test the require_permission FastAPI dependency."""

    @pytest.mark.asyncio
    async def test_allows_with_permission(self):
        token = {"sub": "user-1", "app_metadata": {"favonius_role": "favonius_admin"}}
        dep = require_permission(Permission.ADMIN_CONFIG)

        with patch("src.security.rbac.verify_token", return_value=token):
            await dep(token=token)

    @pytest.mark.asyncio
    async def test_denies_without_permission(self):
        token = {"sub": "user-1", "app_metadata": {"favonius_role": "viewer"}}
        dep = require_permission(Permission.OPTIMIZE_TRIGGER)

        with pytest.raises(HTTPException) as exc_info:
            await dep(token=token)
        assert exc_info.value.status_code == 403


class TestRequireRole:
    """Test the require_role FastAPI dependency."""

    @pytest.mark.asyncio
    async def test_allows_matching_role(self):
        token = {"sub": "user-1", "app_metadata": {"favonius_role": "favonius_admin"}}
        dep = require_role(Role.FAVONIUS_ADMIN)

        with patch("src.security.rbac.verify_token", return_value=token):
            await dep(token=token)

    @pytest.mark.asyncio
    async def test_denies_wrong_role(self):
        token = {"sub": "user-1", "app_metadata": {"favonius_role": "viewer"}}
        dep = require_role(Role.FAVONIUS_ADMIN)

        with pytest.raises(HTTPException) as exc_info:
            await dep(token=token)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_role_denied(self):
        token = {"sub": "user-1", "role": "custom_unknown"}
        dep = require_role(Role.FAVONIUS_ADMIN)

        with pytest.raises(HTTPException):
            await dep(token=token)
