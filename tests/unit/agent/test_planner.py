"""Unit tests for the SQL-mode planner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from src.api.agent.planner import (
    PlannerDecision,
    classify,
    fetch_org_sql_enabled,
    is_sql_mode_enabled,
)

ORG_A = UUID("11111111-1111-1111-1111-111111111111")
ORG_B = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AGENT_SQL_MODE_ENABLED", raising=False)
    is_sql_mode_enabled.cache_clear()


def test_consumption_question_routes_to_fast_path(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("How much did John charge last month?", sql_mode_allowed=True)
    assert d.route == "consumption_by_user"
    assert "consumption" in d.reason


def test_consumption_question_routes_when_sql_mode_off(monkeypatch):
    # SQL mode off — consumption fast path still works.
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "false")
    d = classify("How much did Sarah consume yesterday?", sql_mode_allowed=False)
    assert d.route == "consumption_by_user"


def test_general_question_routes_to_sql_general(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("Which depot consumed the most last week?", sql_mode_allowed=True)
    assert d.route == "sql_general"


def test_general_question_falls_back_to_consumption_when_sql_mode_off(monkeypatch):
    # When SQL mode is off (the default during rollout), non-trigger
    # messages fall back to the consumption_by_user fast path. The
    # intent compiler's LLM extractor then refuses if the message
    # is not a consumption question — preserving the legacy behaviour
    # from before SQL mode existed and keeping all 50 golden
    # consumption prompts on the fast path.
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "false")
    d = classify("Which depot consumed the most?", sql_mode_allowed=False)
    assert d.route == "consumption_by_user"
    assert d.reason == "consumption_fallback_no_sql_mode"


def test_anti_pattern_pulls_consumption_to_sql_general(monkeypatch):
    # "How much did X consume" + "which depot" — the anti-pattern wins.
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify(
        "How much did each driver consume, which depot was highest?",
        sql_mode_allowed=True,
    )
    assert d.route == "sql_general"


def test_sql_mode_allowed_routes_sql_general(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("Which chargers are faulted?", sql_mode_allowed=True)
    assert d.route == "sql_general"


def test_sql_mode_not_allowed_falls_back_to_consumption(monkeypatch):
    # org has sql disabled — fall back to consumption_by_user, not refuse.
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("Which chargers are faulted?", sql_mode_allowed=False)
    assert d.route == "consumption_by_user"
    assert d.reason == "consumption_fallback_no_sql_mode"


def test_empty_message_refused(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("", sql_mode_allowed=True)
    assert d.route == "refuse"
    assert d.reason == "empty_message"


def test_whitespace_only_refused(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("   \n  ", sql_mode_allowed=True)
    assert d.route == "refuse"


def test_sql_mode_truthy_values(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", v)
        is_sql_mode_enabled.cache_clear()
        d = classify("List all chargers", sql_mode_allowed=True)
        assert d.route == "sql_general", f"value {v!r} should enable SQL mode"


# ── fetch_org_sql_enabled unit tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_org_sql_enabled_true():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"agent_sql_mode_enabled": True})
    assert await fetch_org_sql_enabled(pool, ORG_A) is True


@pytest.mark.asyncio
async def test_fetch_org_sql_enabled_false():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"agent_sql_mode_enabled": False})
    assert await fetch_org_sql_enabled(pool, ORG_A) is False


@pytest.mark.asyncio
async def test_fetch_org_sql_enabled_no_org_row_defaults_true():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await fetch_org_sql_enabled(pool, ORG_A) is True


@pytest.mark.asyncio
async def test_fetch_org_sql_enabled_none_org_id_returns_false():
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    assert await fetch_org_sql_enabled(pool, None) is False
    pool.fetchrow.assert_not_called()


def test_planner_decision_is_frozen():
    d = PlannerDecision(route="sql_general", reason="x")
    with pytest.raises(Exception):
        d.route = "refuse"  # type: ignore[misc]


# ── S1 routing gate for the 20-question SQL-mode eval suite ──────────────
#
# Source: PLAN.md §"The 20 questions". One assertion per question, so
# any future change to _CONSUMPTION_TRIGGERS / _CONSUMPTION_ANTIPATTERNS
# that re-routes one of these surfaces immediately. SQL mode is forced
# ON because the eval suite assumes it (PLAN.md S0 sets
# AGENT_SQL_MODE_ENABLED=true in dev).
EVAL_QUESTIONS_ROUTING: tuple[tuple[str, str, str], ...] = (
    # (id, question, expected_route)
    # Energy + cost rollups (7)
    # Vehicle consumption is now first-class on the fast path (it used to
    # fall through to sql_general when the compiler was driver-only).
    ("en_01", "How much energy did vehicle bus_101 consume last month?", "consumption_by_user"),
    ("en_02", "What was the total electricity cost at depot Vilnius last week?", "sql_general"),
    ("en_03", "Which depot had the highest energy consumption in April 2026?", "sql_general"),
    ("en_04", "How many kWh did driver John Smith use this month?", "consumption_by_user"),
    ("en_05", "Show me the top 5 vehicles by total cost in the last 30 days.", "sql_general"),
    (
        "en_06",
        "What's the average energy per charging session at depot Vilnius this month?",
        "sql_general",
    ),
    ("en_07", "Did any session this week cost more than €100?", "sql_general"),
    # Operations status (7)
    ("op_08", "Which chargers were faulted yesterday?", "sql_general"),
    ("op_09", "How many optimization runs went infeasible last week?", "sql_general"),
    ("op_10", "What triggered the last 5 reoptimizations at depot Vilnius?", "sql_general"),
    ("op_11", "Which alerts are still active right now?", "sql_general"),
    ("op_12", "Show me chargers that have been unavailable for more than 24 hours.", "sql_general"),
    (
        "op_13",
        "How many charging sessions did we have yesterday, total and per depot?",
        "sql_general",
    ),
    ("op_14", "Are any depots running in degraded optimization mode?", "sql_general"),
    # Pricing & market context (6)
    ("pr_15", "What were the 5 highest electricity prices last week?", "sql_general"),
    ("pr_16", "How many times did a price spike trigger reoptimization last month?", "sql_general"),
    (
        "pr_17",
        "What was the average price during morning peak (07–09 local) last week?",
        "sql_general",
    ),
    ("pr_18", "Compare today's day-ahead prices vs last Friday's at depot Vilnius.", "sql_general"),
    ("pr_19", "Did the electricity price ever go negative in April?", "sql_general"),
    ("pr_20", "When was the most expensive hour in the past 7 days?", "sql_general"),
)


@pytest.mark.parametrize(
    "qid,question,expected_route",
    EVAL_QUESTIONS_ROUTING,
    ids=[q[0] for q in EVAL_QUESTIONS_ROUTING],
)
def test_eval_suite_question_routing(monkeypatch, qid, question, expected_route):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    is_sql_mode_enabled.cache_clear()
    decision = classify(question, sql_mode_allowed=True)
    assert decision.route == expected_route, (
        f"{qid}: {question!r} routed to {decision.route!r} "
        f"(reason={decision.reason!r}), expected {expected_route!r}"
    )


def test_eval_suite_covers_all_20_questions():
    """Guard against accidental row deletion / dedup in the parametrize."""
    assert len(EVAL_QUESTIONS_ROUTING) == 20
    ids = [q[0] for q in EVAL_QUESTIONS_ROUTING]
    assert len(set(ids)) == 20, f"duplicate ids: {ids}"


# ── Two-way routing table (SQL mode ON) ─────────────────────────────────
#
# The fast-path compiler now services driver, RFID card, vehicle/fleet,
# and depot-wide consumption. So vehicle and fleet consumption questions
# route to ``consumption_by_user``; only shapes the compiler still can't
# serve (rankings, comparisons, non-consumption domains) fall through to
# ``sql_general``. This table pins both directions so a future trigger /
# anti-pattern tweak that mis-routes either surfaces immediately.
SQL_ON_ROUTING: tuple[tuple[str, str], ...] = (
    # → fast path (consumption_by_user)
    ("How much power did the renault vans consume last night?", "consumption_by_user"),
    ("How much did vehicle ABC-123 consume last week?", "consumption_by_user"),
    ("How much did the vehicles consume last week?", "consumption_by_user"),
    ("How much have the vehicles charged this month?", "consumption_by_user"),
    ("How much power was consumed last month?", "consumption_by_user"),
    ("How much energy was used yesterday?", "consumption_by_user"),
    ("How much did John charge last month?", "consumption_by_user"),
    # → SQL mode (sql_general): rankings, comparisons, other domains
    ("Which vehicle consumed the most last week?", "sql_general"),
    ("Compare bus 1 and bus 2 consumption last week.", "sql_general"),
    ("Show me the top 5 vehicles by total cost.", "sql_general"),
    ("Which chargers were faulted yesterday?", "sql_general"),
    ("List all vehicles at depot Vilnius.", "sql_general"),
)


@pytest.mark.parametrize(
    "message,expected_route",
    SQL_ON_ROUTING,
    ids=[m[:40] for m, _ in SQL_ON_ROUTING],
)
def test_two_way_routing_sql_mode_on(monkeypatch, message, expected_route):
    """With SQL mode on, consumption (incl. vehicle/fleet/depot-wide)
    hits the fast path; rankings/comparisons/other fall to sql_general."""
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    is_sql_mode_enabled.cache_clear()
    d = classify(message, sql_mode_allowed=True)
    assert d.route == expected_route, (
        f"{message!r} routed to {d.route!r} (reason={d.reason!r}); " f"expected {expected_route!r}"
    )


@pytest.mark.parametrize(
    "message",
    [
        "How much power did the renault vans consume last night?",
        "How much power was consumed last month?",
        "Which vehicle consumed the most last week?",
        "List all vehicles at depot Vilnius.",
    ],
)
def test_sql_mode_off_routes_everything_to_fast_path(monkeypatch, message):
    """With SQL mode OFF there is no sql_general route: every non-empty
    message falls back to the consumption fast path, whose own extraction
    step refuses what it cannot service (legacy default-off behaviour)."""
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "false")
    is_sql_mode_enabled.cache_clear()
    d = classify(message, sql_mode_allowed=False)
    assert d.route == "consumption_by_user", f"{message!r} routed to {d.route!r}"
