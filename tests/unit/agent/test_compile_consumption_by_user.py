"""Tests for :func:`src.api.agent.intents.consumption_by_user.compile_consumption_by_user`.

The compiler is the deterministic last mile between the LLM tier (plan +
names) and the database. These tests pin the exact SQL the agent ships
to TimescaleDB and the order/shape of every parameter. A regression
here either silently changes which sessions count toward a subject's
consumption or breaks the index choice the production query plan relies
on.

Coverage spans every subject shape: driver/card (the original path),
vehicle/fleet, depot-wide, month bucketing, and the three-state result
summary that keeps "no sessions" distinct from "no energy recorded".
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow
from src.api.agent.intents.base import IntentCompiler
from src.api.agent.intents.consumption_by_user import (
    compile_consumption_by_user,
    summarize_consumption_rows,
)
from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow

# Stable UUIDs make golden-file assertions readable.
DRIVER_A = UUID("11111111-1111-1111-1111-111111111111")
DRIVER_B = UUID("22222222-2222-2222-2222-222222222222")
DRIVER_C = UUID("33333333-3333-3333-3333-333333333333")
CARD_1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CARD_2 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CARD_3 = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
VEHICLE_1 = UUID("e0000001-0000-4000-8000-000000000001")
VEHICLE_2 = UUID("e0000002-0000-4000-8000-000000000002")

EXPECTED_SQL = """
        SELECT
            cs.driver_id,
            cs.card_id,
            DATE_TRUNC('day', cs.start_time AT TIME ZONE $5) AS day_local,
            SUM(cs.energy_delivered_kwh) AS energy_kwh,
            SUM(cs.cost_total)           AS cost_total,
            COUNT(*)                     AS session_count,
            COUNT(cs.energy_delivered_kwh) AS energy_sample_count
        FROM charging_sessions cs
        WHERE (
            cs.driver_id = ANY($1::uuid[])
            OR cs.card_id  = ANY($2::uuid[])
          )
          AND cs.start_time >= $3
          AND cs.start_time <  $4
        GROUP BY cs.driver_id, cs.card_id, day_local
        ORDER BY day_local, cs.driver_id NULLS LAST
"""

EXPECTED_VEHICLE_SQL = """
        SELECT
            cs.vehicle_id,
            DATE_TRUNC('day', cs.start_time AT TIME ZONE $4) AS day_local,
            SUM(cs.energy_delivered_kwh) AS energy_kwh,
            SUM(cs.cost_total)           AS cost_total,
            COUNT(*)                     AS session_count,
            COUNT(cs.energy_delivered_kwh) AS energy_sample_count
        FROM charging_sessions cs
        WHERE (
            cs.vehicle_id = ANY($1::text[])
          )
          AND cs.start_time >= $2
          AND cs.start_time <  $3
        GROUP BY cs.vehicle_id, day_local
        ORDER BY day_local, cs.vehicle_id NULLS LAST
"""

EXPECTED_DEPOT_WIDE_SQL = """
        SELECT
            DATE_TRUNC('day', cs.start_time AT TIME ZONE $4) AS day_local,
            SUM(cs.energy_delivered_kwh) AS energy_kwh,
            SUM(cs.cost_total)           AS cost_total,
            COUNT(*)                     AS session_count,
            COUNT(cs.energy_delivered_kwh) AS energy_sample_count
        FROM charging_sessions cs
        WHERE cs.station_id = ANY($1::text[])
          AND cs.start_time >= $2
          AND cs.start_time <  $3
        GROUP BY day_local
        ORDER BY day_local
"""


def _plan(group_by: list[str] | None = None) -> QueryPlan:
    """A minimal valid plan; subjects don't influence SQL generation."""
    return QueryPlan(
        intent="consumption_by_user",
        subjects=[EntityMention(kind="driver", text="John")],
        time_window=TimeWindow(kind="relative", relative="last_month"),
        group_by=group_by or [],
    )


def _window() -> ResolvedTimeWindow:
    """Pinned UTC bounds for May 2026 in Vilnius local time."""
    return ResolvedTimeWindow(
        start_utc=datetime(2026, 4, 30, 21, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 5, 31, 21, 0, tzinfo=timezone.utc),
        timezone="Europe/Vilnius",
    )


class TestGoldenSql:
    def test_full_payload_matches_pinned_sql(self):
        """A canonical driver input produces exactly the pinned SQL."""
        plan = _plan()
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="John Smith (Vilnius)",
                primary_id=DRIVER_A,
                card_ids=[CARD_1, CARD_2],
            )
        ]
        window = _window()

        sql, params = compile_consumption_by_user(plan, resolved, window)

        assert sql == EXPECTED_SQL
        assert params == [
            [DRIVER_A],
            [CARD_1, CARD_2],
            window.start_utc,
            window.end_utc,
            "Europe/Vilnius",
        ]

    def test_param_order_matches_placeholders(self):
        """``$1..$5`` map to driver_ids, card_ids, start, end, tz in order."""
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="John",
                primary_id=DRIVER_A,
                card_ids=[CARD_1],
            )
        ]
        window = _window()

        _, params = compile_consumption_by_user(_plan(), resolved, window)

        assert isinstance(params[0], list) and params[0] == [DRIVER_A]
        assert isinstance(params[1], list) and params[1] == [CARD_1]
        assert params[2] == window.start_utc
        assert params[3] == window.end_utc
        assert params[4] == "Europe/Vilnius"

    def test_energy_sample_count_is_selected(self):
        """The three-state column must be present for every subject shape."""
        sql, _ = compile_consumption_by_user(
            _plan(),
            [ResolvedEntity(kind="driver", display="John", primary_id=DRIVER_A)],
            _window(),
        )
        assert "COUNT(cs.energy_delivered_kwh) AS energy_sample_count" in sql


class TestDriverOnlyPath:
    def test_driver_with_no_cards_emits_empty_card_array(self):
        """Driver-only filter still passes an empty card_ids array.

        Postgres' ``= ANY(empty[])`` is always false, so the OR branch
        becomes a no-op rather than a syntax error or a missing param.
        """
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="John Smith",
                primary_id=DRIVER_A,
                card_ids=[],
            )
        ]

        sql, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert sql == EXPECTED_SQL
        assert params[0] == [DRIVER_A]
        assert params[1] == []  # empty, not None — keeps the placeholder shape


class TestMixedDriverAndCardsPath:
    def test_single_driver_with_cards_combines_both(self):
        """A driver with assigned cards yields one driver_id and N card_ids."""
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="John Smith (Vilnius)",
                primary_id=DRIVER_A,
                card_ids=[CARD_1, CARD_2, CARD_3],
            )
        ]

        sql, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert sql == EXPECTED_SQL
        assert params[0] == [DRIVER_A]
        assert params[1] == [CARD_1, CARD_2, CARD_3]

    def test_depot_entities_are_ignored(self):
        """Depot entities are not subjects and must not add params.

        (Vehicles, by contrast, ARE subjects now — see TestVehiclePath.)
        """
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="John",
                primary_id=DRIVER_A,
                card_ids=[CARD_1],
            ),
            ResolvedEntity(
                kind="depot",
                display="Vilnius",
                primary_id=UUID("88888888-8888-8888-8888-888888888888"),
            ),
        ]

        sql, params = compile_consumption_by_user(_plan(), resolved, _window())

        # Driver path only; depot UUID not in any param and no vehicle clause.
        assert sql == EXPECTED_SQL
        assert params[0] == [DRIVER_A]
        assert params[1] == [CARD_1]
        assert len(params) == 5


class TestVehiclePath:
    def test_single_vehicle_matches_pinned_sql(self):
        """One resolved vehicle compiles to the vehicle-scoped SQL."""
        resolved = [ResolvedEntity(kind="vehicle", display="Renault Van 1", primary_id=VEHICLE_1)]

        sql, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert sql == EXPECTED_VEHICLE_SQL
        # vehicle_id column is VARCHAR → ids are stringified UUIDs.
        assert params[0] == [str(VEHICLE_1)]
        assert params[1] == _window().start_utc
        assert params[2] == _window().end_utc
        assert params[3] == "Europe/Vilnius"

    def test_fleet_expands_to_all_member_ids(self):
        """A fleet (many resolved vehicles) sums across every member id."""
        resolved = [
            ResolvedEntity(kind="vehicle", display="Renault Van 1", primary_id=VEHICLE_1),
            ResolvedEntity(kind="vehicle", display="Renault Van 2", primary_id=VEHICLE_2),
        ]

        sql, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert sql == EXPECTED_VEHICLE_SQL
        assert params[0] == [str(VEHICLE_1), str(VEHICLE_2)]

    def test_not_found_vehicles_are_skipped(self):
        """A primary_id=None vehicle (not found) is not a subject."""
        resolved = [
            ResolvedEntity(kind="vehicle", display="ghost van", primary_id=None),
            ResolvedEntity(kind="vehicle", display="Renault Van 1", primary_id=VEHICLE_1),
        ]

        _, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert params[0] == [str(VEHICLE_1)]


class TestDriverPlusVehicle:
    def test_both_kinds_combine_with_ored_predicates(self):
        """A mixed driver+vehicle query ORs both filters and groups by both."""
        resolved = [
            ResolvedEntity(kind="driver", display="John", primary_id=DRIVER_A, card_ids=[CARD_1]),
            ResolvedEntity(kind="vehicle", display="Renault Van 1", primary_id=VEHICLE_1),
        ]

        sql, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert "cs.driver_id = ANY($1::uuid[])" in sql
        assert "OR cs.card_id  = ANY($2::uuid[])" in sql
        assert "OR cs.vehicle_id = ANY($3::text[])" in sql
        assert "GROUP BY cs.driver_id, cs.card_id, cs.vehicle_id, day_local" in sql
        assert params == [
            [DRIVER_A],
            [CARD_1],
            [str(VEHICLE_1)],
            _window().start_utc,
            _window().end_utc,
            "Europe/Vilnius",
        ]


class TestDepotWidePath:
    def test_station_ids_produce_depot_wide_sql(self):
        """``station_ids`` compiles the no-subject depot-wide form."""
        sql, params = compile_consumption_by_user(
            _plan(), [], _window(), station_ids=["CP-1", "CP-2"]
        )

        assert sql == EXPECTED_DEPOT_WIDE_SQL
        assert params == [
            ["CP-1", "CP-2"],
            _window().start_utc,
            _window().end_utc,
            "Europe/Vilnius",
        ]

    def test_empty_station_ids_still_compiles(self):
        """A depot with zero chargers compiles (empty ANY → no rows)."""
        sql, params = compile_consumption_by_user(_plan(), [], _window(), station_ids=[])
        assert sql == EXPECTED_DEPOT_WIDE_SQL
        assert params[0] == []


class TestMonthBucket:
    def test_group_by_month_uses_month_truncation(self):
        """group_by=['month'] buckets on month; alias stays ``day_local``."""
        sql, _ = compile_consumption_by_user(
            _plan(group_by=["month"]),
            [ResolvedEntity(kind="driver", display="John", primary_id=DRIVER_A)],
            _window(),
        )
        assert "DATE_TRUNC('month', cs.start_time AT TIME ZONE $5) AS day_local" in sql

    def test_day_wins_when_both_present(self):
        """If both 'day' and 'month' are asked for, day bucketing wins."""
        sql, _ = compile_consumption_by_user(
            _plan(group_by=["month", "day"]),
            [ResolvedEntity(kind="driver", display="John", primary_id=DRIVER_A)],
            _window(),
        )
        assert "DATE_TRUNC('day'," in sql


class TestEmptySubjectList:
    def test_no_resolved_subjects_raises_value_error(self):
        """Caller must surface 'not found' before invoking the compiler."""
        with pytest.raises(ValueError, match="at least one resolved"):
            compile_consumption_by_user(_plan(), [], _window())

    def test_subjects_without_primary_id_raise(self):
        """A 'not found' subject (primary_id=None) is not a subject."""
        resolved = [ResolvedEntity(kind="driver", display="ghost", primary_id=None)]
        with pytest.raises(ValueError, match="at least one resolved"):
            compile_consumption_by_user(_plan(), resolved, _window())

    def test_only_depot_entities_raises(self):
        """A resolved set with only a depot (not a subject kind) raises."""
        resolved = [
            ResolvedEntity(
                kind="depot",
                display="Vilnius",
                primary_id=UUID("88888888-8888-8888-8888-888888888888"),
            )
        ]
        with pytest.raises(ValueError, match="at least one resolved"):
            compile_consumption_by_user(_plan(), resolved, _window())


class TestMultiDriver:
    def test_param_array_preserves_input_order(self):
        """Multi-driver param array has all UUIDs in the input order."""
        resolved = [
            ResolvedEntity(kind="driver", display="A", primary_id=DRIVER_A),
            ResolvedEntity(kind="driver", display="B", primary_id=DRIVER_B),
            ResolvedEntity(kind="driver", display="C", primary_id=DRIVER_C),
        ]

        _, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert params[0] == [DRIVER_A, DRIVER_B, DRIVER_C]

    def test_card_ids_flatten_across_all_drivers_in_order(self):
        """Multiple drivers' cards flatten into a single ordered list."""
        resolved = [
            ResolvedEntity(kind="driver", display="A", primary_id=DRIVER_A, card_ids=[CARD_1]),
            ResolvedEntity(
                kind="driver", display="B", primary_id=DRIVER_B, card_ids=[CARD_2, CARD_3]
            ),
        ]

        _, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert params[0] == [DRIVER_A, DRIVER_B]
        assert params[1] == [CARD_1, CARD_2, CARD_3]


class TestProtocolContract:
    def test_module_imports_cleanly(self):
        """The base ``IntentCompiler`` Protocol imports without error."""
        assert IntentCompiler is not None
        assert hasattr(IntentCompiler, "compile")


class TestNullsLastOrdering:
    def test_sql_orders_by_day_then_driver_nulls_last(self):
        """Rows with ``driver_id IS NULL`` (card-only) bucket at the end."""
        sql, _ = compile_consumption_by_user(
            _plan(),
            [ResolvedEntity(kind="driver", display="John", primary_id=DRIVER_A, card_ids=[CARD_1])],
            _window(),
        )

        assert "ORDER BY day_local, cs.driver_id NULLS LAST" in sql


class TestSummarizeConsumptionRows:
    def test_no_sessions_disposition(self):
        """Empty result → no_sessions, totals are None (not 0)."""
        summary = summarize_consumption_rows([])
        assert summary["disposition"] == "no_sessions"
        assert summary["total_sessions"] == 0
        assert summary["total_energy_kwh"] is None

    def test_no_energy_recorded_disposition(self):
        """Sessions exist but every energy value is NULL → no_energy_recorded."""
        rows = [
            {"session_count": 3, "energy_sample_count": 0, "energy_kwh": None, "cost_total": None},
        ]
        summary = summarize_consumption_rows(rows)
        assert summary["disposition"] == "no_energy_recorded"
        assert summary["total_sessions"] == 3
        assert summary["total_energy_kwh"] is None

    def test_ok_disposition_sums_energy_and_cost(self):
        """Real data → ok, with summed energy and cost."""
        rows = [
            {"session_count": 2, "energy_sample_count": 2, "energy_kwh": 10.5, "cost_total": 3.0},
            {"session_count": 1, "energy_sample_count": 1, "energy_kwh": 4.5, "cost_total": 1.5},
        ]
        summary = summarize_consumption_rows(rows)
        assert summary["disposition"] == "ok"
        assert summary["total_sessions"] == 3
        assert summary["total_energy_kwh"] == 15.0
        assert summary["total_cost"] == 4.5
        assert summary["group_count"] == 2

    def test_partial_energy_still_ok(self):
        """A mix of recorded and unrecorded energy is still 'ok'."""
        rows = [
            {"session_count": 2, "energy_sample_count": 1, "energy_kwh": 7.0, "cost_total": None},
        ]
        summary = summarize_consumption_rows(rows)
        assert summary["disposition"] == "ok"
        assert summary["total_energy_kwh"] == 7.0
        assert summary["total_cost"] is None
