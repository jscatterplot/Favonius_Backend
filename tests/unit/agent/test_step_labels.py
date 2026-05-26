"""Unit tests for the SSE ``step`` friendly-label mapping.

The chat UI renders the ``summary`` of each SSE ``step`` event verbatim, so
it must be plain progress text rather than a raw phase or tool name. These
tests lock that contract and guard against drift when new SQL-mode tools are
added.
"""

from __future__ import annotations

from src.api.agent.controller import (
    _DEFAULT_STEP_LABEL,
    _STEP_LABELS,
    _friendly_step_label,
)
from src.api.agent.sql_tools import SQL_AGENT_TOOL_NAMES


def test_known_phases_map_to_friendly_text() -> None:
    assert _friendly_step_label("extract_plan") == "Working out what you're asking"
    assert _friendly_step_label("resolve_entities") == "Finding who and what you mentioned"
    assert _friendly_step_label("execute") == "Fetching the data"


def test_known_sql_tools_map_to_friendly_text() -> None:
    assert _friendly_step_label("run_select_ts") == "Querying charging & telemetry data"
    assert _friendly_step_label("lookup_entity") == "Finding the matching record"
    assert _friendly_step_label("emit_final_answer") == "Composing your answer"


def test_unknown_step_falls_back_to_generic_label() -> None:
    assert _friendly_step_label("some_future_tool") == _DEFAULT_STEP_LABEL
    assert _friendly_step_label("") == _DEFAULT_STEP_LABEL


def test_consumption_phases_are_all_labelled() -> None:
    for phase in ("planner_decision", "extract_plan", "resolve_entities", "compile", "execute"):
        assert phase in _STEP_LABELS, f"missing label for phase {phase!r}"


def test_every_sql_tool_has_a_label() -> None:
    # Drift guard: a new SQL-mode tool must ship with a friendly label so the
    # chat UI never surfaces a raw function name like ``run_select_ts``.
    missing = [name for name in SQL_AGENT_TOOL_NAMES if name not in _STEP_LABELS]
    assert missing == [], f"SQL tools missing a friendly step label: {missing}"


def test_labels_read_as_prose_not_identifiers() -> None:
    # No snake_case identifiers leaking through, and each label is a sentence
    # fragment that starts with a capital letter.
    for key, label in _STEP_LABELS.items():
        assert "_" not in label, f"label for {key!r} looks like an identifier: {label!r}"
        assert label[:1].isupper(), f"label for {key!r} should start capitalised: {label!r}"
