"""Regression test for `_emit_final_answer` defensive coercion.

Bugbot raised that the inner ``int(row_evidence)`` could raise ValueError
when the LLM passes a non-castable value (e.g. ``"42 rows"``), turning
what should be a clean terminator call into a generic-failure path that
loops silently. The fix coerces with try/except and clamps to a non-
negative int.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.api.agent.sql_tools import (
    EMIT_FINAL_ANSWER_TOOL,
    build_sql_agent_tool_registry,
)


def _build_registry():
    """Build the real registry. The pools are never touched by the
    terminator callable so MagicMock is sufficient."""
    auth = MagicMock()
    auth.visible_depot_ids = []
    static_pool = MagicMock()
    ts_pool = MagicMock()
    return build_sql_agent_tool_registry(static_pool, ts_pool, auth)


@pytest.mark.asyncio
async def test_terminator_accepts_integer_row_evidence():
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": "There were 42 sessions.", "row_evidence": 42},
    )
    assert result == {"text": "There were 42 sessions.", "row_evidence": 42}


@pytest.mark.asyncio
async def test_terminator_coerces_string_row_evidence():
    """LLM sometimes passes ``"42"`` as a string. int("42") works."""
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": "ok", "row_evidence": "42"},
    )
    assert result["row_evidence"] == 42


@pytest.mark.asyncio
async def test_terminator_handles_non_castable_row_evidence():
    """LLM occasionally passes ``"42 rows"`` or other non-numeric strings.
    Pre-fix this raised ValueError, the runtime caught it as a generic
    tool failure, and the loop just continued without any clean signal.
    With the fix, it coerces to 0 and the terminator returns normally.
    """
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": "ok", "row_evidence": "42 rows"},
    )
    assert result == {"text": "ok", "row_evidence": 0}


@pytest.mark.asyncio
async def test_terminator_handles_none_row_evidence():
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": "ok", "row_evidence": None},
    )
    assert result["row_evidence"] == 0


@pytest.mark.asyncio
async def test_terminator_clamps_negative_row_evidence():
    """A negative row count is nonsensical; clamp to 0."""
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": "ok", "row_evidence": -5},
    )
    assert result["row_evidence"] == 0


@pytest.mark.asyncio
async def test_terminator_omitted_row_evidence_defaults_to_zero():
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": "ok"},
    )
    assert result["row_evidence"] == 0


@pytest.mark.asyncio
async def test_terminator_coerces_none_text_to_empty_string():
    """``text`` is required by the schema, but if the LLM sends ``null``
    the runtime should still get a string back, not None."""
    reg = _build_registry()
    result = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"text": None, "row_evidence": 5},
    )
    assert result["text"] == ""
    assert result["row_evidence"] == 5
