"""Tests for the per-kind entity resolvers in ``src.api.agent.resolve``.

These tests mock the static-pool ``fetch`` directly so the per-resolver
SQL parameter shapes can be asserted alongside the resolution
behavior. The SQL strings themselves are verified by execution against
the live ``favonius-pilot`` Supabase project (column / table names);
this suite covers the Python layer on top of that SQL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent.plan import EntityMention
from src.api.agent.resolve import (
    _resolve_depot,
    _resolve_driver,
    _resolve_rfid,
    _resolve_vehicle,
    resolve_entities,
)
from tests.unit.agent.conftest import (
    CUSTOMER_ADMIN_USER_ID,
    FAVONIUS_ADMIN_USER_ID,
    ORG_ID,
)

# All tests in this module exercise async functions; pytest-asyncio is
# in auto mode (see ``pytest.ini``) so the ``asyncio`` marker is
# implicit, but stating it at module scope keeps the file readable
# alongside ``test_auth_context.py``.
pytestmark = pytest.mark.asyncio


# Stable depot UUIDs make assertions readable.
DEPOT_A = UUID("cccccccc-cccc-cccc-cccc-ccccccccccc1")
DEPOT_B = UUID("cccccccc-cccc-cccc-cccc-ccccccccccc2")


def _customer_auth() -> AuthContext:
    return AuthContext(
        user_id=UUID(CUSTOMER_ADMIN_USER_ID),
        organization_id=UUID(ORG_ID),
        role="customer_admin",
        visible_depot_ids=[DEPOT_A, DEPOT_B],
    )


def _admin_auth() -> AuthContext:
    return AuthContext(
        user_id=UUID(FAVONIUS_ADMIN_USER_ID),
        organization_id=None,
        role="favonius_admin",
        visible_depot_ids=[DEPOT_A, DEPOT_B],
    )


# --------------------------------------------------------------------------- #
# Driver resolver
# --------------------------------------------------------------------------- #


class TestResolveDriver:
    async def test_single_match_returns_one_entity(self, fake_static_pool):
        driver_id = uuid4()
        card_id = uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": driver_id,
                    "display_name": "John Smith",
                    "depot_id": DEPOT_A,
                    "external_driver_id": "EMP-1042",
                    "depot_name": "Vilnius",
                    "card_ids": [card_id],
                }
            ]
        )

        result = await _resolve_driver("John", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        entity = result[0]
        assert entity.kind == "driver"
        assert entity.primary_id == driver_id
        assert entity.display == "John Smith (Vilnius)"
        assert entity.card_ids == [card_id]
        assert entity.candidates == []

    async def test_multi_match_returns_head_with_candidates(self, fake_static_pool):
        d1, d2, d3 = uuid4(), uuid4(), uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": d1,
                    "display_name": "John Smith",
                    "depot_id": DEPOT_A,
                    "external_driver_id": "EMP-1042",
                    "depot_name": "Vilnius",
                    "card_ids": [],
                },
                {
                    "driver_id": d2,
                    "display_name": "John Petrauskas",
                    "depot_id": DEPOT_A,
                    "external_driver_id": "EMP-1158",
                    "depot_name": "Vilnius",
                    "card_ids": [],
                },
                {
                    "driver_id": d3,
                    "display_name": "Jonas Brazauskas",
                    "depot_id": DEPOT_B,
                    "external_driver_id": "EMP-1201",
                    "depot_name": "Kaunas",
                    "card_ids": [],
                },
            ]
        )

        result = await _resolve_driver("John", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        head = result[0]
        # Top match is the primary entity.
        assert head.primary_id == d1
        # Candidates list carries head + tail in order.
        assert [c.primary_id for c in head.candidates] == [d1, d2, d3]
        assert head.candidates[0].display == "John Smith (Vilnius)"
        assert head.candidates[2].display == "Jonas Brazauskas (Kaunas)"

    async def test_zero_match_returns_primary_id_none(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(return_value=[])

        result = await _resolve_driver("Nobody", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].kind == "driver"
        assert result[0].primary_id is None
        assert result[0].display == "Nobody"
        assert result[0].card_ids == []
        assert result[0].candidates == []

    async def test_org_scoping_filters_other_orgs(self, fake_static_pool):
        # The mock returns no rows because the other org's depot UUID
        # is not in this caller's visible_depot_ids; the SQL filter
        # ``d.site_id = ANY($1::uuid[])`` is what enforces this in
        # production. The test asserts the scope param is passed
        # through and that "no rows" surfaces as not-found.
        fake_static_pool.fetch = AsyncMock(return_value=[])
        auth = _customer_auth()

        result = await _resolve_driver("John", auth, fake_static_pool)

        assert result[0].primary_id is None
        # The visible_depot_ids list went into $1 unmodified.
        call = fake_static_pool.fetch.await_args
        assert call.args[1] == auth.visible_depot_ids
        # The text ILIKE went into $2 wrapped in '%'.
        assert call.args[2] == "%John%"

    async def test_card_ids_populated_when_assignments_exist(self, fake_static_pool):
        c1, c2 = uuid4(), uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": uuid4(),
                    "display_name": "Maria",
                    "depot_id": DEPOT_A,
                    "external_driver_id": None,
                    "depot_name": "Vilnius",
                    "card_ids": [c1, c2],
                }
            ]
        )

        result = await _resolve_driver("Maria", _customer_auth(), fake_static_pool)

        assert result[0].card_ids == [c1, c2]

    async def test_card_ids_empty_when_no_assignments(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": uuid4(),
                    "display_name": "Maria",
                    "depot_id": DEPOT_A,
                    "external_driver_id": None,
                    "depot_name": "Vilnius",
                    # COALESCE with array_agg(... FILTER WHERE NOT NULL)
                    # yields an empty array, not NULL.
                    "card_ids": [],
                }
            ]
        )

        result = await _resolve_driver("Maria", _customer_auth(), fake_static_pool)

        assert result[0].card_ids == []

    async def test_card_ids_empty_when_array_is_none(self, fake_static_pool):
        # Belt-and-braces: even if the COALESCE in the SQL is removed
        # by accident, the resolver still tolerates NULL → [].
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": uuid4(),
                    "display_name": "Maria",
                    "depot_id": DEPOT_A,
                    "external_driver_id": None,
                    "depot_name": "Vilnius",
                    "card_ids": None,
                }
            ]
        )

        result = await _resolve_driver("Maria", _customer_auth(), fake_static_pool)

        assert result[0].card_ids == []

    async def test_external_driver_id_match(self, fake_static_pool):
        # A user pastes "EMP-1042" — the SQL ORs against
        # ``external_driver_id ILIKE`` so the row comes back.
        driver_id = uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": driver_id,
                    "display_name": "John Smith",
                    "depot_id": DEPOT_A,
                    "external_driver_id": "EMP-1042",
                    "depot_name": "Vilnius",
                    "card_ids": [],
                }
            ]
        )

        result = await _resolve_driver("EMP-1042", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].primary_id == driver_id
        assert result[0].display == "John Smith (Vilnius)"
        # The text was passed wrapped in ILIKE wildcards.
        call = fake_static_pool.fetch.await_args
        assert call.args[2] == "%EMP-1042%"

    async def test_uuid_strings_are_coerced(self, fake_static_pool):
        # asyncpg returns native UUIDs, but tests / dumped fixtures
        # may carry strings; both should land as UUIDs in the model.
        driver_id_str = "11111111-2222-3333-4444-555555555555"
        card_id_str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "driver_id": driver_id_str,
                    "display_name": "John",
                    "depot_id": str(DEPOT_A),
                    "external_driver_id": None,
                    "depot_name": "Vilnius",
                    "card_ids": [card_id_str],
                }
            ]
        )

        result = await _resolve_driver("John", _customer_auth(), fake_static_pool)

        assert result[0].primary_id == UUID(driver_id_str)
        assert result[0].card_ids == [UUID(card_id_str)]


# --------------------------------------------------------------------------- #
# Vehicle resolver
# --------------------------------------------------------------------------- #


class TestResolveVehicle:
    async def test_happy_path_for_tenant_uses_organization_id(self, fake_static_pool):
        vehicle_id = uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "vehicle_id": vehicle_id,
                    "display_text": "Bus 42",
                    "depot_id": DEPOT_A,
                    "depot_name": "Vilnius",
                }
            ]
        )

        result = await _resolve_vehicle("42", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].kind == "vehicle"
        assert result[0].primary_id == vehicle_id
        assert result[0].display == "Bus 42 (Vilnius)"

        call = fake_static_pool.fetch.await_args
        # First param is the org id (vehicles has organization_id natively).
        assert call.args[1] == ORG_ID
        # Second param is the visible-depot scope; third is the ILIKE pattern.
        assert call.args[2] == [DEPOT_A, DEPOT_B]
        assert call.args[3] == "%42%"

    async def test_happy_path_for_admin_uses_visible_depot_ids(self, fake_static_pool):
        vehicle_id = uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "vehicle_id": vehicle_id,
                    "display_text": "VINABCDEFGHI12345",
                    "depot_id": DEPOT_B,
                    "depot_name": "Kaunas",
                }
            ]
        )

        result = await _resolve_vehicle("VINABCDEFGHI", _admin_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].primary_id == vehicle_id

        call = fake_static_pool.fetch.await_args
        # Admin path: $1 is the visible_depot_ids list; no org filter.
        assert call.args[1] == [DEPOT_A, DEPOT_B]
        assert call.args[2] == "%VINABCDEFGHI%"

    async def test_zero_match_returns_primary_id_none(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(return_value=[])

        result = await _resolve_vehicle("ghost-bus", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].kind == "vehicle"
        assert result[0].primary_id is None
        assert result[0].display == "ghost-bus"

    async def test_multi_match_collapses_into_head_with_candidates(self, fake_static_pool):
        v1, v2 = uuid4(), uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "vehicle_id": v1,
                    "display_text": "Bus 42",
                    "depot_id": DEPOT_A,
                    "depot_name": "Vilnius",
                },
                {
                    "vehicle_id": v2,
                    "display_text": "Bus 421",
                    "depot_id": DEPOT_B,
                    "depot_name": "Kaunas",
                },
            ]
        )

        result = await _resolve_vehicle("42", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert [c.primary_id for c in result[0].candidates] == [v1, v2]


# --------------------------------------------------------------------------- #
# Depot resolver
# --------------------------------------------------------------------------- #


class TestResolveDepot:
    async def test_happy_path(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(
            return_value=[{"depot_id": DEPOT_A, "depot_name": "Vilnius depot"}]
        )

        result = await _resolve_depot("Vilnius", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].kind == "depot"
        assert result[0].primary_id == DEPOT_A
        assert result[0].display == "Vilnius depot"
        assert result[0].card_ids == []

        # The visible-depot scope is the only filter beyond the name match.
        call = fake_static_pool.fetch.await_args
        assert call.args[1] == [DEPOT_A, DEPOT_B]
        assert call.args[2] == "%Vilnius%"

    async def test_zero_match(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(return_value=[])

        result = await _resolve_depot("Atlantis", _customer_auth(), fake_static_pool)

        assert result[0].primary_id is None
        assert result[0].display == "Atlantis"


# --------------------------------------------------------------------------- #
# RFID resolver
# --------------------------------------------------------------------------- #


class TestResolveRfid:
    async def test_happy_path_id_tag_match(self, fake_static_pool):
        card_id = uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "card_id": card_id,
                    "id_tag": "DEADBEEF",
                    "label": None,
                    "depot_id": DEPOT_A,
                    "depot_name": "Vilnius",
                }
            ]
        )

        result = await _resolve_rfid("DEADBEEF", _customer_auth(), fake_static_pool)

        assert len(result) == 1
        assert result[0].kind == "rfid"
        assert result[0].primary_id == card_id
        # Display falls back to id_tag when label is NULL.
        assert result[0].display == "DEADBEEF (Vilnius)"

        call = fake_static_pool.fetch.await_args
        # $2 is the exact id_tag; $3 is the ILIKE-wrapped label match.
        assert call.args[2] == "DEADBEEF"
        assert call.args[3] == "%DEADBEEF%"

    async def test_label_match(self, fake_static_pool):
        card_id = uuid4()
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {
                    "card_id": card_id,
                    "id_tag": "AABBCCDD",
                    "label": "Delivery card 3",
                    "depot_id": DEPOT_A,
                    "depot_name": "Vilnius",
                }
            ]
        )

        result = await _resolve_rfid("Delivery", _customer_auth(), fake_static_pool)

        # Display prefers label when set.
        assert result[0].display == "Delivery card 3 (Vilnius)"

    async def test_zero_match(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(return_value=[])

        result = await _resolve_rfid("UNKNOWN", _customer_auth(), fake_static_pool)

        assert result[0].primary_id is None
        assert result[0].display == "UNKNOWN"


# --------------------------------------------------------------------------- #
# Top-level dispatcher
# --------------------------------------------------------------------------- #


class TestResolveEntities:
    async def test_empty_mentions_short_circuits(self, fake_static_pool):
        result = await resolve_entities([], _customer_auth(), fake_static_pool)

        assert result == []
        fake_static_pool.fetch.assert_not_awaited()

    async def test_dispatches_each_kind_to_its_resolver(self, fake_static_pool):
        # Each of the four resolver paths gets one fetch call; we set
        # up four return values via ``side_effect`` to mimic that.
        driver_id = uuid4()
        vehicle_id = uuid4()
        card_id = uuid4()

        fake_static_pool.fetch = AsyncMock(
            side_effect=[
                # driver
                [
                    {
                        "driver_id": driver_id,
                        "display_name": "John Smith",
                        "depot_id": DEPOT_A,
                        "external_driver_id": "EMP-1042",
                        "depot_name": "Vilnius",
                        "card_ids": [card_id],
                    }
                ],
                # vehicle
                [
                    {
                        "vehicle_id": vehicle_id,
                        "display_text": "Bus 42",
                        "depot_id": DEPOT_A,
                        "depot_name": "Vilnius",
                    }
                ],
                # depot
                [{"depot_id": DEPOT_A, "depot_name": "Vilnius depot"}],
                # rfid
                [
                    {
                        "card_id": card_id,
                        "id_tag": "DEADBEEF",
                        "label": None,
                        "depot_id": DEPOT_A,
                        "depot_name": "Vilnius",
                    }
                ],
            ]
        )

        mentions = [
            EntityMention(kind="driver", text="John"),
            EntityMention(kind="vehicle", text="42"),
            EntityMention(kind="depot", text="Vilnius"),
            EntityMention(kind="rfid", text="DEADBEEF"),
        ]
        result = await resolve_entities(mentions, _customer_auth(), fake_static_pool)

        kinds = [r.kind for r in result]
        primary_ids = [r.primary_id for r in result]
        assert kinds == ["driver", "vehicle", "depot", "rfid"]
        assert primary_ids == [driver_id, vehicle_id, DEPOT_A, card_id]
        assert fake_static_pool.fetch.await_count == 4
