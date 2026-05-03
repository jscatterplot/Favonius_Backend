"""Tests for :func:`src.api.agent.auth_context.build_auth_context`.

Each role path goes through one of the two depot queries
(``get_all_depots`` for favonius_admin, ``get_depots_for_organization``
for everyone else); the tests assert which query ran, with which args,
and what shape the resulting :class:`AuthContext` takes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from src.api.agent.auth_context import build_auth_context
from tests.unit.agent.conftest import (
    CUSTOMER_ADMIN_USER_ID,
    CUSTOMER_OPERATOR_USER_ID,
    FAVONIUS_ADMIN_USER_ID,
    ORG_ID,
    OTHER_ORG_ID,
)

# All tests in this module exercise an async function, so apply the
# asyncio marker at module scope rather than per-method.
pytestmark = pytest.mark.asyncio


class TestFavoniusAdmin:
    async def test_returns_all_depots(
        self, fake_static_pool, favonius_admin_payload, depot_row_factory
    ):
        own_depot = depot_row_factory(organization_id=ORG_ID)
        other_depot = depot_row_factory(organization_id=OTHER_ORG_ID)
        fake_static_pool.fetch = AsyncMock(return_value=[own_depot, other_depot])

        ctx = await build_auth_context(favonius_admin_payload, fake_static_pool)

        assert ctx.role == "favonius_admin"
        assert ctx.user_id == UUID(FAVONIUS_ADMIN_USER_ID)
        assert ctx.organization_id is None
        assert set(ctx.visible_depot_ids) == {
            UUID(own_depot["depot_id"]),
            UUID(other_depot["depot_id"]),
        }
        # Single .fetch call without an org filter (get_all_depots).
        assert fake_static_pool.fetch.await_count == 1
        args = fake_static_pool.fetch.await_args.args
        assert len(args) == 1  # query string only

    async def test_admin_with_org_id_keeps_org_id(
        self, fake_static_pool, favonius_admin_payload, depot_row_factory
    ):
        # An admin token can carry organization_id (e.g. when the admin
        # acts within their home org). The agent honours it as informational
        # but still pulls the full depot list.
        favonius_admin_payload["app_metadata"]["organization_id"] = ORG_ID
        fake_static_pool.fetch = AsyncMock(return_value=[depot_row_factory(organization_id=ORG_ID)])

        ctx = await build_auth_context(favonius_admin_payload, fake_static_pool)

        assert ctx.organization_id == UUID(ORG_ID)
        assert len(ctx.visible_depot_ids) == 1


class TestCustomerAdmin:
    async def test_returns_only_org_depots(
        self, fake_static_pool, customer_admin_payload, depot_row_factory
    ):
        depots = [
            depot_row_factory(organization_id=ORG_ID),
            depot_row_factory(organization_id=ORG_ID),
        ]
        fake_static_pool.fetch = AsyncMock(return_value=depots)

        ctx = await build_auth_context(customer_admin_payload, fake_static_pool)

        assert ctx.role == "customer_admin"
        assert ctx.user_id == UUID(CUSTOMER_ADMIN_USER_ID)
        assert ctx.organization_id == UUID(ORG_ID)
        assert set(ctx.visible_depot_ids) == {UUID(d["depot_id"]) for d in depots}
        # get_depots_for_organization passed the org id as $1.
        assert fake_static_pool.fetch.await_count == 1
        args = fake_static_pool.fetch.await_args.args
        assert ORG_ID in args

    async def test_missing_org_id_raises_403(self, fake_static_pool, customer_admin_no_org_payload):
        with pytest.raises(HTTPException) as excinfo:
            await build_auth_context(customer_admin_no_org_payload, fake_static_pool)
        assert excinfo.value.status_code == 403
        # Static pool not consulted when authz fails before the query.
        fake_static_pool.fetch.assert_not_awaited()

    async def test_empty_depot_list_returns_successfully(
        self, fake_static_pool, customer_admin_payload
    ):
        # A freshly-onboarded org with no depots yet: the agent succeeds
        # and the resolver downstream surfaces "not found".
        fake_static_pool.fetch = AsyncMock(return_value=[])

        ctx = await build_auth_context(customer_admin_payload, fake_static_pool)

        assert ctx.visible_depot_ids == []
        assert ctx.organization_id == UUID(ORG_ID)


class TestCustomerOperator:
    async def test_returns_only_org_depots(
        self, fake_static_pool, customer_operator_payload, depot_row_factory
    ):
        depots = [depot_row_factory(organization_id=ORG_ID)]
        fake_static_pool.fetch = AsyncMock(return_value=depots)

        ctx = await build_auth_context(customer_operator_payload, fake_static_pool)

        assert ctx.role == "customer_operator"
        assert ctx.user_id == UUID(CUSTOMER_OPERATOR_USER_ID)
        assert ctx.organization_id == UUID(ORG_ID)
        assert ctx.visible_depot_ids == [UUID(depots[0]["depot_id"])]


class TestRejections:
    async def test_unknown_role_raises_403(self, fake_static_pool):
        payload = {
            "sub": str(uuid4()),
            "role": "authenticated",
            "app_metadata": {"favonius_role": "marketplace_user"},
        }
        with pytest.raises(HTTPException) as excinfo:
            await build_auth_context(payload, fake_static_pool)
        assert excinfo.value.status_code == 403
        fake_static_pool.fetch.assert_not_awaited()

    async def test_missing_sub_raises_401(self, fake_static_pool):
        payload = {
            "role": "authenticated",
            "app_metadata": {"favonius_role": "customer_admin"},
        }
        with pytest.raises(HTTPException) as excinfo:
            await build_auth_context(payload, fake_static_pool)
        assert excinfo.value.status_code == 401
