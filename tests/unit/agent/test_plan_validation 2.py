"""Strict-validation tests for the LLM-facing :mod:`src.api.agent.plan` types.

The whole point of these models is to refuse anything the LLM emits that
isn't on the allowlist — unknown intents, extra fields, structurally
inconsistent time windows. If a test in this file regresses, the agent's
trust boundary has a hole.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow


class TestQueryPlan:
    def test_accepts_minimal_valid_payload(self):
        plan = QueryPlan(
            intent="consumption_by_user",
            subjects=[EntityMention(kind="driver", text="John")],
            time_window=TimeWindow(kind="relative", relative="last_month"),
        )
        assert plan.intent == "consumption_by_user"
        assert plan.subjects[0].text == "John"
        assert plan.time_window.relative == "last_month"
        # group_by defaults to []; never None.
        assert plan.group_by == []

    def test_rejects_unknown_intent(self):
        with pytest.raises(ValidationError) as excinfo:
            QueryPlan(
                intent="schedule_charge",  # not in the v0 literal
                subjects=[EntityMention(kind="driver", text="John")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        # Pydantic's literal_error names the offending field.
        assert "intent" in str(excinfo.value)

    def test_rejects_extra_top_level_field(self):
        with pytest.raises(ValidationError):
            QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="driver", text="John")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
                # Extra field — would be ignored without extra='forbid'.
                depot_id="00000000-0000-0000-0000-000000000000",
            )

    def test_accepts_empty_subjects_list(self):
        # The compiler decides what to do with an empty subject list; the
        # plan type itself does not gate that.
        plan = QueryPlan(
            intent="consumption_by_user",
            subjects=[],
            time_window=TimeWindow(kind="relative", relative="last_month"),
        )
        assert plan.subjects == []

    def test_accepts_known_group_by_values(self):
        plan = QueryPlan(
            intent="consumption_by_user",
            subjects=[EntityMention(kind="driver", text="John")],
            time_window=TimeWindow(kind="relative", relative="last_month"),
            group_by=["driver", "category"],
        )
        assert plan.group_by == ["driver", "category"]

    def test_rejects_unknown_group_by_value(self):
        with pytest.raises(ValidationError):
            QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="driver", text="John")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
                group_by=["vehicle"],  # not in the v0 literal
            )


class TestEntityMention:
    def test_accepts_known_kinds(self):
        for kind in ("driver", "vehicle", "depot", "rfid"):
            assert EntityMention(kind=kind, text="x").kind == kind

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            EntityMention(kind="organization", text="Acme")

    def test_rejects_extra_field(self):
        with pytest.raises(ValidationError):
            EntityMention(kind="driver", text="John", driver_id="42")


class TestTimeWindowRelative:
    def test_accepts_each_relative_value(self):
        for value in (
            "last_month",
            "this_month",
            "last_week",
            "this_week",
            "today",
            "yesterday",
        ):
            tw = TimeWindow(kind="relative", relative=value)
            assert tw.relative == value

    def test_requires_relative_field(self):
        with pytest.raises(ValidationError) as excinfo:
            TimeWindow(kind="relative")
        assert "relative" in str(excinfo.value)

    def test_rejects_when_from_iso_and_to_iso_also_set(self):
        with pytest.raises(ValidationError):
            TimeWindow(
                kind="relative",
                relative="last_month",
                from_iso="2026-04-01",
                to_iso="2026-05-01",
            )

    def test_rejects_when_only_from_iso_also_set(self):
        # The contract is "relative mode → no absolute bounds at all", not
        # just "no full pair". One stray bound still indicates confusion.
        with pytest.raises(ValidationError):
            TimeWindow(
                kind="relative",
                relative="last_month",
                from_iso="2026-04-01",
            )

    def test_rejects_unknown_relative_value(self):
        with pytest.raises(ValidationError):
            TimeWindow(kind="relative", relative="last_quarter")

    def test_rejects_extra_field(self):
        with pytest.raises(ValidationError):
            TimeWindow(
                kind="relative",
                relative="last_month",
                tz="Europe/Vilnius",
            )


class TestTimeWindowAbsolute:
    def test_accepts_full_absolute_pair(self):
        tw = TimeWindow(
            kind="absolute",
            from_iso="2026-04-01",
            to_iso="2026-05-01",
        )
        assert tw.from_iso == "2026-04-01"
        assert tw.to_iso == "2026-05-01"

    def test_requires_from_iso_and_to_iso(self):
        with pytest.raises(ValidationError):
            TimeWindow(kind="absolute")

    def test_requires_from_iso(self):
        with pytest.raises(ValidationError):
            TimeWindow(kind="absolute", to_iso="2026-05-01")

    def test_requires_to_iso(self):
        with pytest.raises(ValidationError):
            TimeWindow(kind="absolute", from_iso="2026-04-01")

    def test_rejects_when_relative_also_set(self):
        with pytest.raises(ValidationError):
            TimeWindow(
                kind="absolute",
                from_iso="2026-04-01",
                to_iso="2026-05-01",
                relative="last_month",
            )


class TestTimeWindowKind:
    def test_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            TimeWindow(kind="rolling", relative="last_month")
