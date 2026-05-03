"""Snapshot tests for ``extract_plan`` against the live Anthropic API.

Gated by the ``snapshot`` pytest marker. NOT run on every CI build:

    pytest -m snapshot tests/unit/agent/snapshot/

The fixture set lives at ``tests/unit/agent/snapshot/extract_plan.yaml``.
Each entry maps a natural-language message to the expected
:class:`QueryPlan` shape. The suite uses the model selected by
``AGENT_LLM_MODEL`` so we can compare output quality across models by
re-running with different env values.

Skipped automatically when ``ANTHROPIC_API_KEY`` is unset, so the file
is safe to leave in the tree on every PR — it only fires when an
operator opts in.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.api.agent.llm import extract_plan
from src.api.agent.plan import QueryPlan

pytestmark = [pytest.mark.snapshot, pytest.mark.asyncio]


_FIXTURE_PATH = Path(__file__).parent / "extract_plan.yaml"


def _load_cases() -> list[dict[str, Any]]:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data.get("cases", []))


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; snapshot run is opt-in",
)
@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["message"][:40])
async def test_extract_plan_matches_snapshot(case: dict[str, Any]):
    expected = QueryPlan.model_validate(case["expected"])
    actual = await extract_plan(case["message"])

    # We compare the structural shape (intent, time-window kind, subject
    # kinds) — not the literal ``text`` of every subject, because the
    # LLM may legitimately copy "John" vs "John Smith" depending on
    # prompt wording. For refusal cases (subjects=[]) the empty list is
    # the contract, so check that exactly.
    assert actual.intent == expected.intent
    assert actual.time_window.kind == expected.time_window.kind
    if expected.time_window.kind == "relative":
        assert actual.time_window.relative == expected.time_window.relative

    if not expected.subjects:
        assert actual.subjects == [], f"Expected refusal (subjects=[]) but got {actual.subjects}"
    else:
        assert len(actual.subjects) == len(expected.subjects)
        assert {s.kind for s in actual.subjects} == {s.kind for s in expected.subjects}
