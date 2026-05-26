"""Unit tests for the depot-wide consumption helper in the orchestrator.

``_depot_wide_consumption_rows`` is the no-subject aggregation path: it
scopes to the caller's depots (by ``site_id``) and their chargers (by
``station_id``) — the auth boundary — and runs one query per depot
timezone group. These tests drive it with fake asyncpg pools (no DB) so
the scoping, per-timezone fan-out, and empty-depot fallback are all
covered offline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from src.api.agent import resolve as resolve_mod
from src.api.agent.auth_context import AuthContext
from src.api.agent.controller import _depot_wide_consumption_rows
from src.api.agent.plan import QueryPlan, TimeWindow

pytestmark = pytest.mark.asyncio

ORG = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DEPOT_A = UUID("11111111-1111-4111-8111-111111111111")
DEPOT_B = UUID("22222222-2222-4222-8222-222222222222")


def _auth(depots: list[UUID]) -> AuthContext:
    return AuthContext(
        user_id=UUID("aa000000-0000-4000-8000-000000000001"),
        organization_id=ORG,
        role="customer_admin",
        visible_depot_ids=depots,
    )


def _plan() -> QueryPlan:
    return QueryPlan(
        intent="consumption_by_user",
        subjects=[],
        time_window=TimeWindow(kind="relative", relative="last_month"),
        depot_wide=True,
    )


def _make_static_pool(tz_rows: list[dict], station_rows: list[dict]) -> Any:
    """Fake static pool: sniffs the SQL to return tz vs station rows."""

    async def _fetch(sql: str, *args: Any) -> list[dict]:
        s = sql.lower()
        if "charging_stations" in s:
            return list(station_rows)
        if "from sites" in s:
            return list(tz_rows)
        return []

    pool = MagicMock()
    pool.fetch = _fetch
    return pool


class _FakeTsPool:
    """Fake time-series pool that records every fetch and returns canned rows."""

    def __init__(self, rows_per_call: list[list[dict]]) -> None:
        self._rows_per_call = rows_per_call
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict]:
        idx = len(self.calls)
        self.calls.append((sql, params))
        return self._rows_per_call[idx] if idx < len(self._rows_per_call) else []


@pytest.fixture(autouse=True)
def _clear_tz_cache():
    resolve_mod._clear_tz_cache()
    yield
    resolve_mod._clear_tz_cache()


async def test_single_timezone_single_query_scoped_to_station_ids():
    static_pool = _make_static_pool(
        tz_rows=[{"id": DEPOT_A, "timezone": "Europe/Vilnius"}],
        station_rows=[
            {"ocpp_id": "CP-1", "depot_id": DEPOT_A, "timezone": "Europe/Vilnius"},
            {"ocpp_id": "CP-2", "depot_id": DEPOT_A, "timezone": "Europe/Vilnius"},
        ],
    )
    ts_pool = _FakeTsPool([[{"day_local": "2026-04-01", "session_count": 4}]])

    rows, window, tz_groups = await _depot_wide_consumption_rows(
        static_pool, ts_pool, _auth([DEPOT_A]), _plan()
    )

    assert tz_groups == 1
    assert len(ts_pool.calls) == 1
    sql, params = ts_pool.calls[0]
    assert "cs.site_id = ANY($1::uuid[])" in sql
    assert "cs.station_id = ANY($2::text[])" in sql
    assert params[0] == [DEPOT_A]  # site_id linkage (import rows)
    assert params[1] == ["CP-1", "CP-2"]  # station_id linkage (live OCPP rows)
    assert window.timezone == "Europe/Vilnius"
    assert rows == [{"day_local": "2026-04-01", "session_count": 4}]


async def test_multi_timezone_runs_one_query_per_group():
    static_pool = _make_static_pool(
        tz_rows=[
            {"id": DEPOT_A, "timezone": "Europe/Vilnius"},
            {"id": DEPOT_B, "timezone": "Europe/Helsinki"},
        ],
        station_rows=[
            {"ocpp_id": "CP-A", "depot_id": DEPOT_A, "timezone": "Europe/Vilnius"},
            {"ocpp_id": "CP-B", "depot_id": DEPOT_B, "timezone": "Europe/Helsinki"},
        ],
    )
    ts_pool = _FakeTsPool(
        [
            [{"day_local": "2026-04-01", "session_count": 2}],
            [{"day_local": "2026-04-02", "session_count": 3}],
        ]
    )

    rows, _window, tz_groups = await _depot_wide_consumption_rows(
        static_pool, ts_pool, _auth([DEPOT_A, DEPOT_B]), _plan()
    )

    assert tz_groups == 2
    assert len(ts_pool.calls) == 2
    # Each group is scoped to only its own chargers (station ids are $2)
    # and its own depot (site_id is $1).
    scoped_stations = {call[1][1][0] for call in ts_pool.calls}
    assert scoped_stations == {"CP-A", "CP-B"}
    scoped_depots = {call[1][0][0] for call in ts_pool.calls}
    assert scoped_depots == {DEPOT_A, DEPOT_B}
    # Rows from both groups are concatenated.
    assert len(rows) == 2


async def test_depot_without_chargers_still_queried_by_site_id():
    """A depot with imported history but no registered chargers is still
    queried — matched by ``site_id`` — so XLSX-onboarded depots aren't
    silently dropped from depot-wide totals.
    """
    static_pool = _make_static_pool(
        tz_rows=[{"id": DEPOT_A, "timezone": "Europe/Vilnius"}],
        station_rows=[],  # no chargers (e.g. history-only XLSX onboarding)
    )
    ts_pool = _FakeTsPool([[{"day_local": "2026-04-01", "session_count": 7}]])

    rows, window, tz_groups = await _depot_wide_consumption_rows(
        static_pool, ts_pool, _auth([DEPOT_A]), _plan()
    )

    assert tz_groups == 1
    assert len(ts_pool.calls) == 1
    _sql, params = ts_pool.calls[0]
    assert params[0] == [DEPOT_A]  # site_id scope catches imported rows
    assert params[1] == []  # no chargers to scope station_id by
    assert rows == [{"day_local": "2026-04-01", "session_count": 7}]
    assert window.timezone == "Europe/Vilnius"


async def test_depot_ids_scopes_to_named_subset():
    """``depot_ids`` (a resolved depot subject) scopes to that depot only."""
    static_pool = _make_static_pool(
        tz_rows=[
            {"id": DEPOT_A, "timezone": "Europe/Vilnius"},
            {"id": DEPOT_B, "timezone": "Europe/Vilnius"},
        ],
        station_rows=[
            {"ocpp_id": "CP-A", "depot_id": DEPOT_A, "timezone": "Europe/Vilnius"},
            {"ocpp_id": "CP-B", "depot_id": DEPOT_B, "timezone": "Europe/Vilnius"},
        ],
    )
    ts_pool = _FakeTsPool([[{"day_local": "2026-04-01", "session_count": 1}]])

    # Caller can see both depots, but the question named only DEPOT_A.
    rows, _window, tz_groups = await _depot_wide_consumption_rows(
        static_pool, ts_pool, _auth([DEPOT_A, DEPOT_B]), _plan(), depot_ids=[DEPOT_A]
    )

    assert tz_groups == 1
    assert len(ts_pool.calls) == 1
    _sql, params = ts_pool.calls[0]
    assert params[0] == [DEPOT_A]  # scoped to the named depot only
    assert params[1] == ["CP-A"]  # only DEPOT_A's charger, not CP-B
    assert len(rows) == 1


async def test_depot_ids_outside_visible_set_are_dropped():
    """A depot id not in ``visible_depot_ids`` can never widen scope."""
    static_pool = _make_static_pool(
        tz_rows=[{"id": DEPOT_A, "timezone": "Europe/Vilnius"}],
        station_rows=[{"ocpp_id": "CP-A", "depot_id": DEPOT_A, "timezone": "Europe/Vilnius"}],
    )
    ts_pool = _FakeTsPool([])

    # Caller sees only DEPOT_A; a (spoofed) DEPOT_B is requested.
    rows, _window, tz_groups = await _depot_wide_consumption_rows(
        static_pool, ts_pool, _auth([DEPOT_A]), _plan(), depot_ids=[DEPOT_B]
    )

    assert tz_groups == 0
    assert len(ts_pool.calls) == 0
    assert rows == []
