"""Tests for :func:`src.api.agent.intents.consumption_by_user.compile_consumption_by_user`.

The compiler is the deterministic last mile between the LLM tier (plan +
names) and the database. These tests pin the exact SQL the agent ships
to TimescaleDB and the order/shape of every parameter. A regression
here either silently changes which sessions count toward a driver's
consumption or breaks the index choice the production query plan
relies on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow
from src.api.agent.intents.base import IntentCompiler
from src.api.agent.intents.consumption_by_user import compile_consumption_by_user
from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow

# Stable UUIDs make golden-file assertions readable.
DRIVER_A = UUID("11111111-1111-1111-1111-111111111111")
DRIVER_B = UUID("22222222-2222-2222-2222-222222222222")
DRIVER_C = UUID("33333333-3333-3333-3333-333333333333")
CARD_1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CARD_2 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CARD_3 = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

EXPECTED_SQL = """
        SELECT
            cs.driver_id,
            cs.card_id,
            DATE_TRUNC('day', cs.start_time AT TIME ZONE $5) AS day_local,
            SUM(cs.energy_delivered_kwh) AS energy_kwh,
            SUM(cs.cost_total)           AS cost_total,
            COUNT(*)                     AS session_count
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


def _plan() -> QueryPlan:
    """A minimal valid plan; details don't influence SQL generation in v0."""
    return QueryPlan(
        intent="consumption_by_user",
        subjects=[EntityMention(kind="driver", text="John")],
        time_window=TimeWindow(kind="relative", relative="last_month"),
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
        """A canonical input produces exactly the architecture-doc SQL."""
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

    def test_non_driver_resolved_entities_are_ignored(self):
        """Vehicle / depot / rfid entities don't leak into the SQL params.

        v0 only consumes ``kind='driver'``; later intents will accept
        other kinds. The compiler must filter explicitly so a sloppy
        upstream resolver can't change the WHERE-clause subjects.
        """
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="John",
                primary_id=DRIVER_A,
                card_ids=[CARD_1],
            ),
            ResolvedEntity(
                kind="vehicle",
                display="Bus 42",
                primary_id=UUID("99999999-9999-9999-9999-999999999999"),
            ),
            ResolvedEntity(
                kind="depot",
                display="Vilnius",
                primary_id=UUID("88888888-8888-8888-8888-888888888888"),
            ),
        ]

        _, params = compile_consumption_by_user(_plan(), resolved, _window())

        # Driver ids only; vehicle / depot UUIDs not in any param.
        assert params[0] == [DRIVER_A]
        assert params[1] == [CARD_1]


class TestEmptyDriverList:
    def test_no_resolved_drivers_raises_value_error(self):
        """Caller must surface 'not found' before invoking the compiler."""
        with pytest.raises(ValueError, match="at least one resolved driver"):
            compile_consumption_by_user(_plan(), [], _window())

    def test_drivers_without_primary_id_are_ignored_and_raise(self):
        """A 'not found' driver entity (primary_id=None) is not a subject."""
        resolved = [
            ResolvedEntity(
                kind="driver",
                display="ghost driver",
                primary_id=None,
            )
        ]
        with pytest.raises(ValueError, match="at least one resolved driver"):
            compile_consumption_by_user(_plan(), resolved, _window())

    def test_only_non_driver_entities_raises(self):
        """Resolved set with no driver kind at all also raises."""
        resolved = [
            ResolvedEntity(
                kind="vehicle",
                display="Bus 42",
                primary_id=UUID("99999999-9999-9999-9999-999999999999"),
            )
        ]
        with pytest.raises(ValueError, match="at least one resolved driver"):
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
            ResolvedEntity(
                kind="driver",
                display="A",
                primary_id=DRIVER_A,
                card_ids=[CARD_1],
            ),
            ResolvedEntity(
                kind="driver",
                display="B",
                primary_id=DRIVER_B,
                card_ids=[CARD_2, CARD_3],
            ),
        ]

        _, params = compile_consumption_by_user(_plan(), resolved, _window())

        assert params[0] == [DRIVER_A, DRIVER_B]
        assert params[1] == [CARD_1, CARD_2, CARD_3]

    def test_window_threaded_unchanged_for_multi_driver(self):
        """Window bounds and tz are not recomputed per driver."""
        resolved = [
            ResolvedEntity(kind="driver", display="A", primary_id=DRIVER_A),
            ResolvedEntity(kind="driver", display="B", primary_id=DRIVER_B),
        ]
        window = _window()

        _, params = compile_consumption_by_user(_plan(), resolved, window)

        assert params[2] == window.start_utc
        assert params[3] == window.end_utc
        assert params[4] == window.timezone


class TestProtocolContract:
    def test_module_imports_cleanly(self):
        """The base ``IntentCompiler`` Protocol imports without error.

        Protocol bodies don't execute at runtime, but importing the module
        catches typos in the type imports the contract depends on.
        """
        assert IntentCompiler is not None
        # The Protocol declares one ``compile`` method; sanity-check it
        # exists so a future rename of the canonical method on
        # consumption_by_user.compile_consumption_by_user can be paired
        # with a Protocol update.
        assert hasattr(IntentCompiler, "compile")


class TestNullsLastOrdering:
    def test_sql_orders_by_day_then_driver_nulls_last(self):
        """Rows with ``driver_id IS NULL`` (card-only) bucket at the end.

        The formatter narrates per-driver; trailing NULL-driver rows
        are then surfaced as 'on cards not currently assigned' rather
        than scrambling the per-driver order Postgres' default
        NULLS FIRST would produce.
        """
        sql, _ = compile_consumption_by_user(
            _plan(),
            [
                ResolvedEntity(
                    kind="driver",
                    display="John",
                    primary_id=DRIVER_A,
                    card_ids=[CARD_1],
                )
            ],
            _window(),
        )

        assert "ORDER BY day_local, cs.driver_id NULLS LAST" in sql
