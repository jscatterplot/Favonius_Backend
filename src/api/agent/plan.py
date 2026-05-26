"""LLM-facing types — names and time phrases only, no IDs.

The model produces a :class:`QueryPlan` containing only natural-language
subjects (names, descriptions) and either a relative or absolute time
phrase. UUIDs are produced exclusively server-side by the resolver, never
by the model.

Pydantic with ``extra='forbid'`` is the gate that keeps anything else from
slipping through. If the model emits an unexpected field, a free-form
``intent`` value, or a structurally inconsistent :class:`TimeWindow`,
validation fails and the turn aborts before any database access.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EntityMention(BaseModel):
    """A single subject extracted from the user's message.

    ``kind`` is restricted to the four entity classes the resolver knows
    how to look up. ``text`` carries the raw phrase ("John", "bus 42",
    "Vilnius depot") for the resolver to ILIKE against.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["driver", "vehicle", "depot", "rfid"]
    text: str


class TimeWindow(BaseModel):
    """Either a relative phrase or an absolute date pair.

    Exactly one shape is valid per instance:

    - ``kind='relative'`` → ``relative`` is required; ``from_iso`` and
      ``to_iso`` must both be unset.
    - ``kind='absolute'`` → ``from_iso`` and ``to_iso`` are both required;
      ``relative`` must be unset.

    Server-side resolution (depot timezone → UTC bounds) happens in
    ``src/api/agent/resolve.py``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["relative", "absolute"]
    relative: Optional[
        Literal[
            "last_month",
            "this_month",
            "last_week",
            "this_week",
            "today",
            "yesterday",
        ]
    ] = None
    from_iso: Optional[str] = None
    to_iso: Optional[str] = None

    @model_validator(mode="after")
    def _check_kind_consistency(self) -> "TimeWindow":
        if self.kind == "relative":
            if self.relative is None:
                raise ValueError("TimeWindow(kind='relative') requires the 'relative' field")
            if self.from_iso is not None or self.to_iso is not None:
                raise ValueError(
                    "TimeWindow(kind='relative') must not also set 'from_iso' or 'to_iso'"
                )
        else:
            if self.from_iso is None or self.to_iso is None:
                raise ValueError(
                    "TimeWindow(kind='absolute') requires both 'from_iso' and 'to_iso'"
                )
            if self.relative is not None:
                raise ValueError("TimeWindow(kind='absolute') must not also set 'relative'")
        return self


class QueryPlan(BaseModel):
    """The structured plan extracted from a user message.

    v0 ships with a single intent (``consumption_by_user``); the literal
    expands as new intents land. ``group_by`` is optional and defaults to
    an empty list — the compiler picks sensible defaults per intent when
    the user does not specify a grouping.

    ``depot_wide`` is the explicit signal for a no-named-subject
    consumption question ("how much power was consumed last month").
    It exists to disambiguate two meanings of an empty ``subjects``
    list: ``subjects=[], depot_wide=True`` is an in-scope depot-total
    query the compiler services by summing every session at the
    caller's visible depots; ``subjects=[], depot_wide=False`` remains
    the extraction stage's out-of-scope / refusal signal.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Literal["consumption_by_user"]
    subjects: list[EntityMention]
    time_window: TimeWindow
    group_by: list[Literal["driver", "vehicle", "depot", "day", "month", "category"]] = Field(
        default_factory=list
    )
    depot_wide: bool = False
