"""Module-import behavior tests for :mod:`src.api.agent.llm`.

Two narrow assertions, kept in their own file:

1. The module logs the active model on import so ops can grep
   ``container logs | grep "agent LLM configured"`` to confirm which
   model is in production.
2. Bad ``AGENT_LLM_MODEL`` values fail the process at import time, not
   on the first request — typos should not slip into a deploy.

These tests rely on :func:`importlib.reload`, which mutates
``llm_module.__dict__`` in place. Co-locating them with the rest of the
``extract_plan`` / ``format_answer`` tests confused cross-test state
(the imported function references stayed valid but their module's
globals were re-bound mid-test). The split keeps each file's mocking
strategy uniform.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

import src.api.agent.llm as llm_module


def test_logs_active_model_on_import(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("AGENT_LLM_MODEL", "claude-haiku-4-5")
    try:
        with caplog.at_level("INFO", logger="src.api.agent.llm"):
            importlib.reload(llm_module)
        # The startup log line should mention the configured model so
        # an operator can grep for it in container logs.
        assert any("claude-haiku-4-5" in record.getMessage() for record in caplog.records), [
            r.getMessage() for r in caplog.records
        ]
    finally:
        monkeypatch.delenv("AGENT_LLM_MODEL", raising=False)
        importlib.reload(llm_module)


def test_module_import_fails_loud_on_bad_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_LLM_MODEL", "totally-not-a-claude-model")
    try:
        with pytest.raises((ValueError, ValidationError)):
            importlib.reload(llm_module)
    finally:
        # Restore the module to a sane state for downstream tests in
        # the same worker — ``importlib.reload`` leaves the broken
        # module in sys.modules at the failed state otherwise.
        monkeypatch.delenv("AGENT_LLM_MODEL", raising=False)
        importlib.reload(llm_module)
