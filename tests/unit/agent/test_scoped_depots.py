"""Unit tests for ``_resolve_scoped_depots`` — the LLM-free depot scoping the
deterministic readiness/savings handlers use to honour a depot named in the
message (Codex P2 #2/#3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent.controller import _resolve_scoped_depots

VIL = UUID("11111111-1111-4111-8111-111111111111")
KAU = UUID("22222222-2222-4222-8222-222222222222")
ORG = UUID("bb000000-0000-4000-8000-0000000000bb")
USER = UUID("aa000000-0000-4000-8000-0000000000aa")


def _auth(visible):
    return AuthContext(
        user_id=USER, organization_id=ORG, role="customer_operator", visible_depot_ids=visible
    )


def _pool(rows):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


@pytest.mark.asyncio
async def test_single_visible_depot_skips_query():
    pool = _pool([])
    out = await _resolve_scoped_depots("How much did we save overnight?", _auth([VIL]), pool)
    assert out == [VIL]
    pool.fetch.assert_not_called()  # no need to disambiguate one depot


@pytest.mark.asyncio
async def test_named_depot_scopes_to_it():
    pool = _pool([{"id": VIL, "name": "Vilnius"}, {"id": KAU, "name": "Kaunas"}])
    out = await _resolve_scoped_depots("Is Vilnius ready to depart?", _auth([VIL, KAU]), pool)
    assert out == [VIL]


@pytest.mark.asyncio
async def test_no_named_depot_returns_all_visible():
    pool = _pool([{"id": VIL, "name": "Vilnius"}, {"id": KAU, "name": "Kaunas"}])
    out = await _resolve_scoped_depots("Are we ready to depart?", _auth([VIL, KAU]), pool)
    assert set(out) == {VIL, KAU}


@pytest.mark.asyncio
async def test_multiple_named_depots_scope_to_both():
    pool = _pool([{"id": VIL, "name": "Vilnius"}, {"id": KAU, "name": "Kaunas"}])
    out = await _resolve_scoped_depots(
        "Compare Vilnius and Kaunas savings overnight", _auth([VIL, KAU]), pool
    )
    assert set(out) == {VIL, KAU}


@pytest.mark.asyncio
async def test_word_boundary_avoids_false_substring_match():
    # A depot named "Main" must not match inside "remaining".
    pool = _pool([{"id": VIL, "name": "Main"}, {"id": KAU, "name": "Kaunas"}])
    out = await _resolve_scoped_depots("what is the remaining readiness?", _auth([VIL, KAU]), pool)
    assert set(out) == {VIL, KAU}  # no real match → all visible
