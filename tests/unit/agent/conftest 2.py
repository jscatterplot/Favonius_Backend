"""Shared fixtures for the depot agent unit tests.

The agent has no LLM, no live DB, and no FastAPI app under test in this
sprint (B1). Fixtures here are limited to a fake static-pool whose
``fetch`` is an :class:`AsyncMock` (so tests can configure the rows the
``get_all_depots`` / ``get_depots_for_organization`` queries see) and
sample JWT payloads for each role the agent supports.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# Stable UUIDs make assertions readable.
FAVONIUS_ADMIN_USER_ID = "11111111-1111-1111-1111-111111111111"
CUSTOMER_ADMIN_USER_ID = "22222222-2222-2222-2222-222222222222"
CUSTOMER_OPERATOR_USER_ID = "33333333-3333-3333-3333-333333333333"
ORG_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ORG_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def fake_static_pool() -> Any:
    """A fake asyncpg pool whose ``fetch`` returns configurable rows.

    Tests typically reassign ``pool.fetch = AsyncMock(return_value=...)``
    after acquiring the fixture, so the same fixture serves the
    favonius_admin and customer_admin paths interchangeably.
    """
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


def _depot_row(depot_id: str | None = None, organization_id: str = ORG_ID) -> dict:
    """Build a depot row in the shape ``get_*_depots`` returns."""
    return {
        "depot_id": depot_id or str(uuid4()),
        "organization_id": organization_id,
        "name": "Vilnius depot",
        "latitude": None,
        "longitude": None,
        "timezone": "Europe/Vilnius",
        "currency": "EUR",
        "utility_id": None,
        "max_grid_kw": 250.0,
        "demand_charge_rate_kw": 20.0,
        "demand_charge_billing_period": "monthly",
        "address": {},
        "billing_metadata": {},
        "building_load_source": {},
    }


@pytest.fixture
def depot_row_factory():
    """Factory fixture for building depot rows in tests."""
    return _depot_row


@pytest.fixture
def favonius_admin_payload() -> dict:
    """A platform-admin JWT payload (cross-org scope, no organization_id)."""
    return {
        "sub": FAVONIUS_ADMIN_USER_ID,
        "role": "authenticated",
        "app_metadata": {"favonius_role": "favonius_admin"},
    }


@pytest.fixture
def customer_admin_payload() -> dict:
    """A tenant-admin JWT payload bound to ``ORG_ID``."""
    return {
        "sub": CUSTOMER_ADMIN_USER_ID,
        "role": "authenticated",
        "app_metadata": {
            "favonius_role": "customer_admin",
            "organization_id": ORG_ID,
        },
    }


@pytest.fixture
def customer_operator_payload() -> dict:
    """A tenant-operator JWT payload bound to ``ORG_ID``."""
    return {
        "sub": CUSTOMER_OPERATOR_USER_ID,
        "role": "authenticated",
        "app_metadata": {
            "favonius_role": "customer_operator",
            "organization_id": ORG_ID,
        },
    }


@pytest.fixture
def customer_admin_no_org_payload() -> dict:
    """Customer admin without an organization — must trip the 403 branch."""
    return {
        "sub": CUSTOMER_ADMIN_USER_ID,
        "role": "authenticated",
        "app_metadata": {"favonius_role": "customer_admin"},
    }
