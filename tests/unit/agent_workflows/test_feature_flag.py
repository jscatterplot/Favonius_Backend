"""Feature-flag tests for :mod:`src.api.agent_workflows`."""

from __future__ import annotations

import pytest

from src.api.agent_workflows.feature_flag import is_depot_agent_enabled


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DEPOT_AGENT_ENABLED", raising=False)
    assert is_depot_agent_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_enabled_by_env(monkeypatch, value):
    monkeypatch.setenv("DEPOT_AGENT_ENABLED", value)
    assert is_depot_agent_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "off", "no", ""])
def test_disabled_for_unrecognised(monkeypatch, value):
    monkeypatch.setenv("DEPOT_AGENT_ENABLED", value)
    assert is_depot_agent_enabled() is False
