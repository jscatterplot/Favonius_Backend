"""Integration tests for the 8 recurring-schedule endpoints under
``/admin/depots/{depot_id}/schedule/recurring``.

Backend half of the contract with Favonius Frontend PR #119:

    GET    /schedule/recurring                                  → list templates
    POST   /schedule/recurring                                  → create
    PATCH  /schedule/recurring/{template_id}                    → update
    DELETE /schedule/recurring/{template_id}                    → delete
    POST   /schedule/recurring/{template_id}/pause              → active=false
    POST   /schedule/recurring/{template_id}/resume             → active=true
    POST   /schedule/recurring/{template_id}/occurrences/.../cancel
    DELETE /schedule/recurring/{template_id}/occurrences/.../cancel

The pool is mocked with a tiny in-memory store so insert/list/update/delete
flow round-trips inside one test. The pure expansion logic is exercised in
``tests/unit/test_recurring_schedule_expansion.py``.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

# Stub pyomo before any src.* imports (matches the pattern used by other
# integration tests).
if "pyomo" not in sys.modules:
    _pyomo_mock = _MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock

from datetime import date, datetime, time as _time, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.api.main import app
from src.security.tenant_mirror import ensure_tenant_mirrored

AUTH_HDR = {"Authorization": "Bearer test-token"}


# --------------------------------------------------------------------------- #
# In-memory store
# --------------------------------------------------------------------------- #


class _TemplateStore:
    """Tiny in-memory mock of public.recurring_schedule_template +
    public.recurring_schedule_cancellation."""

    def __init__(self, depot_id: str, vehicle_id: str) -> None:
        self.depot_id = UUID(depot_id)
        self.vehicle_id = UUID(vehicle_id)
        self.templates: dict[str, dict[str, Any]] = {}
        self.cancellations: dict[tuple[str, date], dict[str, Any]] = {}

    def insert_template(self, **fields: Any) -> dict[str, Any]:
        template_id = str(uuid4())
        now = datetime.now(timezone.utc)
        row = {
            "id": UUID(template_id),
            "depot_id": fields["depot_id"],
            "vehicle_id": fields["vehicle_id"],
            "route_id": fields["route_id"],
            "departure_time_of_day": fields["departure_time_of_day"],
            "return_time_of_day": fields["return_time_of_day"],
            "days_of_week": list(fields["days_of_week"]),
            "start_date": fields["start_date"],
            "end_date": fields["end_date"],
            "required_soc": fields["required_soc"],
            "energy_kwh": fields["energy_kwh"],
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        self.templates[template_id] = row
        return row

    def update_template(self, template_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        existing = self.templates.get(template_id)
        if existing is None:
            return None
        existing.update(fields)
        existing["updated_at"] = datetime.now(timezone.utc)
        return existing

    def delete_template(self, template_id: str) -> int:
        if template_id in self.templates:
            del self.templates[template_id]
            # cascade
            self.cancellations = {
                key: val for key, val in self.cancellations.items()
                if key[0] != template_id
            }
            return 1
        return 0

    def upsert_cancellation(
        self,
        template_id: str,
        occurrence_date: date,
        cancelled_by_user_id: Optional[UUID],
        reason: Optional[str],
    ) -> dict[str, Any]:
        row = {
            "template_id": UUID(template_id),
            "occurrence_date": occurrence_date,
            "cancelled_at": datetime.now(timezone.utc),
            "cancelled_by_user_id": cancelled_by_user_id,
            "reason": reason,
        }
        self.cancellations[(template_id, occurrence_date)] = row
        return row

    def delete_cancellation(self, template_id: str, occurrence_date: date) -> int:
        key = (template_id, occurrence_date)
        if key in self.cancellations:
            del self.cancellations[key]
            return 1
        return 0


# --------------------------------------------------------------------------- #
# Pool builder
# --------------------------------------------------------------------------- #


def _build_static_pool(store: _TemplateStore) -> tuple[MagicMock, AsyncMock]:
    """Build a fake static pool whose acquire() yields a stateful AsyncMock conn.

    The mock interprets the SQL fragment to dispatch to the in-memory store.
    """
    conn = AsyncMock()

    async def conn_fetch(query: str, *args, **kwargs):
        q = query.strip()
        if q.startswith("SELECT id, depot_id, vehicle_id, route_id,") and "FROM recurring_schedule_template" in q:
            if "AND active = TRUE" in q:
                # fetch_recurring_horizon_data path — not exercised by these
                # endpoint tests but supported for completeness.
                depot_id = args[0]
                return [
                    {**row, "id": row["id"]} for row in store.templates.values()
                    if row["depot_id"] == depot_id and row["active"]
                ]
            # list_recurring_templates: ordered by created_at ASC
            depot_id = args[0]
            return sorted(
                (row for row in store.templates.values() if row["depot_id"] == depot_id),
                key=lambda r: r["created_at"],
            )
        if "FROM recurring_schedule_cancellation" in q and "template_id = ANY" in q:
            template_ids = {str(t) for t in args[0]}
            return [
                row for (tid, _d), row in store.cancellations.items()
                if tid in template_ids
            ]
        if "FROM vehicles" in q and "id = ANY" in q:
            depot_id = args[0]
            requested = {str(v) for v in args[1]}
            if depot_id == store.depot_id and str(store.vehicle_id) in requested:
                return [{"vehicle_id": str(store.vehicle_id)}]
            return []
        return []

    async def conn_fetchrow(query: str, *args, **kwargs):
        q = query.strip()
        if q.startswith("INSERT INTO recurring_schedule_template"):
            row = store.insert_template(
                depot_id=args[0],
                vehicle_id=args[1],
                route_id=args[2],
                departure_time_of_day=args[3],
                return_time_of_day=args[4],
                days_of_week=args[5],
                start_date=args[6],
                end_date=args[7],
                required_soc=args[8],
                energy_kwh=args[9],
            )
            return row
        if q.startswith("UPDATE recurring_schedule_template"):
            template_id = str(args[0])
            depot_id = args[1]
            existing = store.templates.get(template_id)
            if existing is None or existing["depot_id"] != depot_id:
                return None
            return store.update_template(
                template_id,
                vehicle_id=args[2],
                route_id=args[3],
                departure_time_of_day=args[4],
                return_time_of_day=args[5],
                days_of_week=args[6],
                start_date=args[7],
                end_date=args[8],
                required_soc=args[9],
                energy_kwh=args[10],
                active=args[11],
            )
        if q.startswith("SELECT id, depot_id, vehicle_id, route_id,") and "WHERE id = $1 AND depot_id = $2" in q:
            template_id = str(args[0])
            depot_id = args[1]
            existing = store.templates.get(template_id)
            if existing and existing["depot_id"] == depot_id:
                return existing
            return None
        if q.startswith("INSERT INTO recurring_schedule_cancellation"):
            return store.upsert_cancellation(
                template_id=str(args[0]),
                occurrence_date=args[1],
                cancelled_by_user_id=args[2],
                reason=args[3],
            )
        return None

    async def conn_execute(query: str, *args, **kwargs):
        q = query.strip()
        if q.startswith("DELETE FROM recurring_schedule_template"):
            count = store.delete_template(str(args[0]))
            return f"DELETE {count}"
        if q.startswith("DELETE FROM recurring_schedule_cancellation"):
            count = store.delete_cancellation(str(args[0]), args[1])
            return f"DELETE {count}"
        return "DELETE 0"

    async def conn_fetchval(query: str, *args, **kwargs):
        # Used by _build_depot_readiness_checklist EXISTS probes. Return
        # truthy so the readiness payload doesn't 500 on a missing depot.
        q = query.strip()
        if "FROM sites" in q and "charger_vehicle_access_default" in q:
            return "all_to_all"
        if q.startswith("SELECT EXISTS"):
            return True
        return None

    conn.fetch.side_effect = conn_fetch
    conn.fetchrow.side_effect = conn_fetchrow
    conn.execute.side_effect = conn_execute
    conn.fetchval.side_effect = conn_fetchval
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__.return_value = None
    conn.transaction.return_value.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None

    pools = MagicMock()
    pools.static = pool
    # ts pool is read by _build_depot_readiness_checklist (prices, building_load
    # EXISTS probes). Reuse the same conn so the EXISTS branch above runs.
    pools.ts = pool
    return pools, conn


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def bypass_geo_block():
    _not_blocked = MagicMock(blocked=False)
    with patch("src.security.geo_block.check_ip_blocked", return_value=_not_blocked):
        yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def _user(org_id: str, role: str = "customer_admin") -> dict:
    return {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": role, "organization_id": org_id},
    }


def _override(user: dict):
    def _impl():
        return user
    return _impl


@pytest.fixture
def world():
    depot_id = str(uuid4())
    org_id = str(uuid4())
    vehicle_id = str(uuid4())
    store = _TemplateStore(depot_id=depot_id, vehicle_id=vehicle_id)
    pools, conn = _build_static_pool(store)
    app.dependency_overrides[ensure_tenant_mirrored] = _override(_user(org_id))
    return {
        "depot_id": depot_id,
        "org_id": org_id,
        "vehicle_id": vehicle_id,
        "store": store,
        "pools": pools,
        "conn": conn,
    }


def _patches(pools: MagicMock):
    return patch.multiple(
        "src.api.main",
        db_pools=pools,
        verify_depot_access=AsyncMock(return_value=None),
    )


# --------------------------------------------------------------------------- #
# Happy-path round trips
# --------------------------------------------------------------------------- #


def test_create_list_patch_pause_resume_cancel_uncancel_delete_round_trip(client, world):
    """One canonical journey through every endpoint."""
    depot_id = world["depot_id"]
    vehicle_id = world["vehicle_id"]
    base = f"/admin/depots/{depot_id}/schedule/recurring"

    with _patches(world["pools"]):
        # 1. POST → create
        create_body = {
            "vehicle_id": vehicle_id,
            "route_id": "R12",
            "departure_time_of_day": "07:30",
            "return_time_of_day": "19:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
            "start_date": "2026-05-22",
            "end_date": None,
            "required_soc": 1.0,
            "energy_kwh": 180,
        }
        r1 = client.post(base, headers=AUTH_HDR, json=create_body)
        assert r1.status_code == status.HTTP_201_CREATED, r1.text
        created = r1.json()["created"]
        assert created["route_id"] == "R12"
        assert created["departure_time_of_day"] == "07:30"
        assert created["return_time_of_day"] == "19:00"
        assert created["crosses_midnight"] is False
        assert created["cancelled_dates"] == []
        assert created["active"] is True
        template_id = created["template_id"]

        # 2. GET → list returns the row
        r2 = client.get(base, headers=AUTH_HDR)
        assert r2.status_code == 200
        assert len(r2.json()["templates"]) == 1
        assert r2.json()["templates"][0]["template_id"] == template_id

        # 3. PATCH → change route_id + add end_date
        r3 = client.patch(
            f"{base}/{template_id}",
            headers=AUTH_HDR,
            json={"route_id": "R13", "end_date": "2026-12-31"},
        )
        assert r3.status_code == 200, r3.text
        updated = r3.json()["updated"]
        assert updated["route_id"] == "R13"
        assert updated["end_date"] == "2026-12-31"

        # 4. POST /pause → active=false
        r4 = client.post(f"{base}/{template_id}/pause", headers=AUTH_HDR)
        assert r4.status_code == 200
        assert r4.json()["updated"]["active"] is False

        # 5. POST /resume → active=true
        r5 = client.post(f"{base}/{template_id}/resume", headers=AUTH_HDR)
        assert r5.status_code == 200
        assert r5.json()["updated"]["active"] is True

        # 6. POST /occurrences/{date}/cancel
        cancel_url = f"{base}/{template_id}/occurrences/2026-06-23/cancel"
        r6 = client.post(cancel_url, headers=AUTH_HDR, json={"reason": "Vehicle service"})
        assert r6.status_code == 200, r6.text
        assert r6.json()["occurrence_date"] == "2026-06-23"
        assert r6.json()["reason"] == "Vehicle service"

        # Cancelled date is inlined on the next list response.
        r6b = client.get(base, headers=AUTH_HDR)
        assert r6b.json()["templates"][0]["cancelled_dates"] == ["2026-06-23"]

        # 7. DELETE /occurrences/{date}/cancel → un-cancel
        r7 = client.delete(cancel_url, headers=AUTH_HDR)
        assert r7.status_code == 200
        r7b = client.get(base, headers=AUTH_HDR)
        assert r7b.json()["templates"][0]["cancelled_dates"] == []

        # 8. DELETE → template removed
        r8 = client.delete(f"{base}/{template_id}", headers=AUTH_HDR)
        assert r8.status_code == 200
        r8b = client.get(base, headers=AUTH_HDR)
        assert r8b.json()["templates"] == []


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_non_admin_role_is_forbidden(client, world):
    """customer_operator/viewer must not create templates."""
    depot_id = world["depot_id"]
    app.dependency_overrides[ensure_tenant_mirrored] = _override(
        _user(world["org_id"], role="customer_operator")
    )
    with _patches(world["pools"]):
        r = client.post(
            f"/admin/depots/{depot_id}/schedule/recurring",
            headers=AUTH_HDR,
            json={
                "vehicle_id": world["vehicle_id"],
                "route_id": "R1",
                "departure_time_of_day": "07:30",
                "return_time_of_day": "19:00",
                "days_of_week": ["mon"],
                "start_date": "2026-05-22",
                "required_soc": 1.0,
            },
        )
    assert r.status_code == status.HTTP_403_FORBIDDEN


def test_missing_org_id_is_forbidden(client, world):
    depot_id = world["depot_id"]
    no_org = {"sub": str(uuid4()), "app_metadata": {"favonius_role": "customer_admin"}}
    app.dependency_overrides[ensure_tenant_mirrored] = _override(no_org)
    with _patches(world["pools"]):
        r = client.get(
            f"/admin/depots/{depot_id}/schedule/recurring", headers=AUTH_HDR
        )
    assert r.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Validation envelope
# --------------------------------------------------------------------------- #


def _post_create(client, world, body: dict):
    with _patches(world["pools"]):
        return client.post(
            f"/admin/depots/{world['depot_id']}/schedule/recurring",
            headers=AUTH_HDR,
            json=body,
        )


def test_bad_hhmm_returns_400_validation_error(client, world):
    r = _post_create(client, world, {
        "vehicle_id": world["vehicle_id"],
        "route_id": "R1",
        "departure_time_of_day": "7:30",   # missing leading zero → invalid
        "return_time_of_day": "19:00",
        "days_of_week": ["mon"],
        "start_date": "2026-05-22",
        "required_soc": 1.0,
    })
    assert r.status_code == 400
    payload = r.json()
    assert payload["error_code"] == "VALIDATION_ERROR"
    assert "departure_time_of_day" in payload["field_errors"]


def test_invalid_day_returns_400(client, world):
    r = _post_create(client, world, {
        "vehicle_id": world["vehicle_id"],
        "route_id": "R1",
        "departure_time_of_day": "07:30",
        "return_time_of_day": "19:00",
        "days_of_week": ["MON"],   # uppercase rejected
        "start_date": "2026-05-22",
        "required_soc": 1.0,
    })
    assert r.status_code == 400
    assert "days_of_week" in r.json()["field_errors"]


def test_end_date_before_start_date_returns_400(client, world):
    r = _post_create(client, world, {
        "vehicle_id": world["vehicle_id"],
        "route_id": "R1",
        "departure_time_of_day": "07:30",
        "return_time_of_day": "19:00",
        "days_of_week": ["mon"],
        "start_date": "2026-05-22",
        "end_date": "2026-05-01",  # before start
        "required_soc": 1.0,
    })
    assert r.status_code == 400
    assert r.json()["error_code"] == "VALIDATION_ERROR"


def test_identical_times_return_400(client, world):
    r = _post_create(client, world, {
        "vehicle_id": world["vehicle_id"],
        "route_id": "R1",
        "departure_time_of_day": "07:30",
        "return_time_of_day": "07:30",
        "days_of_week": ["mon"],
        "start_date": "2026-05-22",
        "required_soc": 1.0,
    })
    assert r.status_code == 400


def test_vehicle_not_in_depot_returns_400(client, world):
    """Vehicle that exists but isn't in this depot fails the membership check."""
    other_vehicle = str(uuid4())
    r = _post_create(client, world, {
        "vehicle_id": other_vehicle,
        "route_id": "R1",
        "departure_time_of_day": "07:30",
        "return_time_of_day": "19:00",
        "days_of_week": ["mon"],
        "start_date": "2026-05-22",
        "required_soc": 1.0,
    })
    assert r.status_code == 400
    assert "vehicle_id" in r.json()["field_errors"]


def test_required_soc_below_floor_returns_400(client, world):
    r = _post_create(client, world, {
        "vehicle_id": world["vehicle_id"],
        "route_id": "R1",
        "departure_time_of_day": "07:30",
        "return_time_of_day": "19:00",
        "days_of_week": ["mon"],
        "start_date": "2026-05-22",
        "required_soc": 0.5,
    })
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# 404 paths
# --------------------------------------------------------------------------- #


def test_patch_unknown_template_returns_404(client, world):
    depot_id = world["depot_id"]
    with _patches(world["pools"]):
        r = client.patch(
            f"/admin/depots/{depot_id}/schedule/recurring/{uuid4()}",
            headers=AUTH_HDR,
            json={"route_id": "R-new"},
        )
    assert r.status_code == 404


def test_delete_unknown_template_returns_404(client, world):
    depot_id = world["depot_id"]
    with _patches(world["pools"]):
        r = client.delete(
            f"/admin/depots/{depot_id}/schedule/recurring/{uuid4()}",
            headers=AUTH_HDR,
        )
    assert r.status_code == 404


def test_cancel_unknown_template_returns_404(client, world):
    depot_id = world["depot_id"]
    with _patches(world["pools"]):
        r = client.post(
            f"/admin/depots/{depot_id}/schedule/recurring/{uuid4()}/occurrences/2026-06-23/cancel",
            headers=AUTH_HDR,
            json={},
        )
    assert r.status_code == 404


def test_uncancel_unknown_template_returns_404(client, world):
    depot_id = world["depot_id"]
    with _patches(world["pools"]):
        r = client.delete(
            f"/admin/depots/{depot_id}/schedule/recurring/{uuid4()}/occurrences/2026-06-23/cancel",
            headers=AUTH_HDR,
        )
    assert r.status_code == 404


def test_cancel_with_bad_date_returns_400(client, world):
    """Path-param date must be YYYY-MM-DD."""
    depot_id = world["depot_id"]
    # Create one template so the auth/path resolves to the date check.
    with _patches(world["pools"]):
        r0 = client.post(
            f"/admin/depots/{depot_id}/schedule/recurring",
            headers=AUTH_HDR,
            json={
                "vehicle_id": world["vehicle_id"],
                "route_id": "R1",
                "departure_time_of_day": "07:30",
                "return_time_of_day": "19:00",
                "days_of_week": ["mon"],
                "start_date": "2026-05-22",
                "required_soc": 1.0,
            },
        )
        template_id = r0.json()["created"]["template_id"]
        r = client.post(
            f"/admin/depots/{depot_id}/schedule/recurring/{template_id}/occurrences/not-a-date/cancel",
            headers=AUTH_HDR,
            json={},
        )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error_code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# Crosses-midnight returned correctly
# --------------------------------------------------------------------------- #


def test_crosses_midnight_flag_round_trips(client, world):
    """Returning before departing in HH:MM order → crosses_midnight=true on the wire."""
    depot_id = world["depot_id"]
    with _patches(world["pools"]):
        r = client.post(
            f"/admin/depots/{depot_id}/schedule/recurring",
            headers=AUTH_HDR,
            json={
                "vehicle_id": world["vehicle_id"],
                "route_id": "Night-1",
                "departure_time_of_day": "22:00",
                "return_time_of_day": "03:00",
                "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                "start_date": "2026-05-22",
                "required_soc": 1.0,
            },
        )
    assert r.status_code == 201
    assert r.json()["created"]["crosses_midnight"] is True
