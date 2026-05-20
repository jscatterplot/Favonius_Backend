"""Unit tests for the SQL-mode planner."""

from __future__ import annotations

from uuid import UUID

import pytest

from src.api.agent.planner import classify, PlannerDecision


ORG_A = UUID("11111111-1111-1111-1111-111111111111")
ORG_B = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AGENT_SQL_MODE_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_SQL_ORG_ALLOWLIST", raising=False)


def test_consumption_question_routes_to_fast_path(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("How much did John charge last month?", organization_id=ORG_A)
    assert d.route == "consumption_by_user"
    assert "consumption" in d.reason


def test_consumption_question_routes_when_sql_mode_off(monkeypatch):
    # SQL mode off — consumption fast path still works.
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "false")
    d = classify("How much did Sarah consume yesterday?", organization_id=ORG_A)
    assert d.route == "consumption_by_user"


def test_general_question_routes_to_sql_general(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("Which depot consumed the most last week?", organization_id=ORG_A)
    assert d.route == "sql_general"


def test_general_question_refused_when_sql_mode_off(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "false")
    d = classify("Which depot consumed the most?", organization_id=ORG_A)
    assert d.route == "refuse"
    assert d.reason == "sql_mode_disabled"


def test_anti_pattern_pulls_consumption_to_sql_general(monkeypatch):
    # "How much did X consume" + "which depot" — the anti-pattern wins.
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify(
        "How much did each driver consume, which depot was highest?",
        organization_id=ORG_A,
    )
    assert d.route == "sql_general"


def test_allowlist_match_routes_sql_general(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    monkeypatch.setenv("AGENT_SQL_ORG_ALLOWLIST", str(ORG_A))
    d = classify("Which chargers are faulted?", organization_id=ORG_A)
    assert d.route == "sql_general"


def test_allowlist_miss_refuses(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    monkeypatch.setenv("AGENT_SQL_ORG_ALLOWLIST", str(ORG_B))
    d = classify("Which chargers are faulted?", organization_id=ORG_A)
    assert d.route == "refuse"


def test_allowlist_no_org_id_refuses(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    monkeypatch.setenv("AGENT_SQL_ORG_ALLOWLIST", str(ORG_A))
    d = classify("Which chargers?", organization_id=None)
    assert d.route == "refuse"


def test_empty_message_refused(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("", organization_id=ORG_A)
    assert d.route == "refuse"
    assert d.reason == "empty_message"


def test_whitespace_only_refused(monkeypatch):
    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    d = classify("   \n  ", organization_id=ORG_A)
    assert d.route == "refuse"


def test_sql_mode_truthy_values(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", v)
        d = classify("List all chargers", organization_id=ORG_A)
        assert d.route == "sql_general", f"value {v!r} should enable SQL mode"


def test_planner_decision_is_frozen():
    d = PlannerDecision(route="sql_general", reason="x")
    with pytest.raises(Exception):
        d.route = "refuse"  # type: ignore[misc]
