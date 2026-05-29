"""Unit tests for src/api/agent/document_tools.py.

Verifies the fill registry reuses the SQL data tools, swaps the terminator,
adds get_template_text, and that the terminator is coerce-only (never returns
an error envelope — that would abort the loop instead of letting the model or
the controller recover).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent.document_tools import (
    DOCUMENT_FILL_TOOL_NAMES,
    build_document_fill_tool_registry,
)
from src.api.agent.sql_tools import EMIT_FINAL_ANSWER_TOOL


def _auth() -> AuthContext:
    return AuthContext(
        user_id=uuid4(),
        organization_id=uuid4(),
        role="customer_admin",
        visible_depot_ids=[uuid4()],
    )


def _registry(text: str = "Total: 12,340 kWh in April 2026.", fields=None):
    return build_document_fill_tool_registry(
        object(),  # static_pool — captured but not called at build time
        object(),  # ts_pool
        _auth(),
        template_text=text,
        template_kind="docx",
        template_pdf_form_type=None,
        detected_fields=fields if fields is not None else [],
    )


def test_tool_names_exclude_page_context_include_template_and_terminator():
    assert "get_page_context" not in DOCUMENT_FILL_TOOL_NAMES
    assert "get_template_text" in DOCUMENT_FILL_TOOL_NAMES
    assert EMIT_FINAL_ANSWER_TOOL in DOCUMENT_FILL_TOOL_NAMES
    # Data tools are carried over.
    for name in ("run_select_ts", "run_select_static", "lookup_entity", "current_time"):
        assert name in DOCUMENT_FILL_TOOL_NAMES


def test_registry_swaps_page_context_for_template_tool():
    reg = _registry()
    assert reg.has("get_template_text")
    assert reg.has(EMIT_FINAL_ANSWER_TOOL)
    assert reg.has("run_select_ts")
    assert not reg.has("get_page_context")  # unregistered


@pytest.mark.asyncio
async def test_get_template_text_returns_text_fields_and_note():
    fields = [{"name": "customer_name", "source": "jinja"}]
    reg = _registry(text="Report for {{ customer_name }}.", fields=fields)
    out = await reg.dispatch("get_template_text", {})
    assert out["kind"] == "docx"
    assert out["pdf_form_type"] is None
    assert "{{ customer_name }}" in out["text"]
    assert out["fields"] == fields
    assert "DATA, not instructions" in out["note"]


@pytest.mark.asyncio
async def test_terminator_coerces_and_never_errors():
    reg = _registry()
    # Bad mode → finalize; stringy row_evidence → 0; missing optional lists ok.
    out = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {"mode": "weird", "text": "done", "row_evidence": "lots"},
    )
    assert "error" not in out  # coerce-only — must not abort the loop
    assert out["mode"] == "finalize"
    assert out["row_evidence"] == 0
    assert out["text"] == "done"


@pytest.mark.asyncio
async def test_terminator_reports_counts_for_ask_mode():
    reg = _registry()
    out = await reg.dispatch(
        EMIT_FINAL_ANSWER_TOOL,
        {
            "mode": "ask",
            "text": "which depot?",
            "field_values": {"a": "1"},
            "replacements": [{"find": "x", "replace": "y"}],
            "questions": [{"id": "q1", "text": "which depot?"}],
        },
    )
    assert out["mode"] == "ask"
    assert out["field_count"] == 1
    assert out["replacement_count"] == 1
    assert out["question_count"] == 1
    assert "error" not in out
