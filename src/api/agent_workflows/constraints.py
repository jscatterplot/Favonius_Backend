"""Hard-constraint guard for the workflow agent (PRD §10.3).

The agent and the optimiser share a constraints layer. These are
*constraints*, not objectives — no agent proposal at any permission tier
may violate them. The guard runs in two places inside one turn:

1. Before dispatching any tool call, against the LLM's tool input. A
   violating call is rejected with a synthetic ``tool_result`` so the
   LLM sees the failure and can react.
2. After the LLM emits its terminal structured output, against the
   ``proposed_actions`` array. Violating entries are filtered out and
   moved into ``filtered_violations`` on the Decision's ``output`` for
   audit visibility.

The substrate is the source of truth for the *enforcement* of these
constraints — the optimiser still rejects infeasible plans at solve
time, OCPP dispatch still refuses to send a profile that would breach
``max_grid_kw``. The guard here is a belt-and-braces layer: it prevents
a constraint-violating action from ever leaving the agent's mouth so
human reviewers don't have to spot it.

The detection is intentionally schema-light. It walks an action dict
looking for fields whose names are conventionally used for the two
hard quantities the PRD calls out:

- Departure SoC fields: ``departure_soc``, ``target_soc``,
  ``required_soc``, ``soc_at_departure``. Values ``> 1`` are treated as
  percentages and divided by 100; values in ``[0, 1]`` are treated as
  fractions. This mirrors existing SoC normalisation in the codebase.
- Grid power fields: ``grid_kw``, ``grid_power_kw``, ``site_grid_kw``,
  ``p_grid_kw``. The corresponding cap is the depot's ``max_grid_kw``;
  if it isn't known to the guard, the cap is not enforced (the
  optimiser still does).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Optional

# Field names the guard recognises as "departure SoC" / "grid power".
# Keeping these as module-level tuples (not regex) keeps the detection
# auditable and trivially explainable in a PRD review.
_SOC_FIELDS: tuple[str, ...] = (
    "departure_soc",
    "target_soc",
    "required_soc",
    "soc_at_departure",
    "min_departure_soc",
)
_GRID_KW_FIELDS: tuple[str, ...] = (
    "grid_kw",
    "grid_power_kw",
    "site_grid_kw",
    "p_grid_kw",
    "site_kw",
)


@dataclass(frozen=True)
class ConstraintViolation:
    """One detected violation. Recorded in audit; never silently dropped."""

    constraint: str  # "departure_soc_min" | "grid_power_max"
    field: str
    value: float
    limit: float
    detail: str


@dataclass(frozen=True)
class DepotConstraints:
    """Per-depot constraint values.

    ``min_departure_soc`` is a fraction in ``[0, 1]``. ``max_grid_kw``
    is in kilowatts; ``None`` means "not known to the guard, skip the
    check" — the optimiser still enforces it at solve time.
    """

    min_departure_soc: float = 0.99
    max_grid_kw: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)


class HardConstraintGuard:
    """Detects and filters hard-constraint violations in agent outputs."""

    def __init__(self, constraints: DepotConstraints | None = None) -> None:
        self.constraints = constraints or DepotConstraints()

    # ── Detection ────────────────────────────────────────────────────────

    def validate_action(self, action: dict[str, Any]) -> Optional[ConstraintViolation]:
        """Return the first violation found in ``action``, or ``None``.

        The first-violation contract is deliberate: a single rejection
        message is easier for the LLM to act on than a batch.
        """
        if not isinstance(action, dict):
            return None

        for fname in _SOC_FIELDS:
            if fname in action:
                v = _coerce_float(action[fname])
                if v is None:
                    continue
                fraction = v / 100.0 if v > 1.0 else v
                if fraction < self.constraints.min_departure_soc:
                    return ConstraintViolation(
                        constraint="departure_soc_min",
                        field=fname,
                        value=v,
                        limit=self.constraints.min_departure_soc,
                        detail=(
                            f"{fname}={v} (fraction={fraction:.3f}) is below "
                            f"the hard minimum {self.constraints.min_departure_soc}"
                        ),
                    )

        if self.constraints.max_grid_kw is not None:
            for fname in _GRID_KW_FIELDS:
                if fname in action:
                    v = _coerce_float(action[fname])
                    if v is None:
                        continue
                    if v > self.constraints.max_grid_kw:
                        return ConstraintViolation(
                            constraint="grid_power_max",
                            field=fname,
                            value=v,
                            limit=self.constraints.max_grid_kw,
                            detail=(
                                f"{fname}={v} exceeds the depot's "
                                f"max_grid_kw={self.constraints.max_grid_kw}"
                            ),
                        )
        return None

    # ── Filtering ────────────────────────────────────────────────────────

    def filter_actions(
        self,
        actions: Iterable[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[ConstraintViolation]]:
        """Split actions into (kept, violations).

        Order is preserved among kept actions so a workflow's prioritised
        list of proposals doesn't get scrambled by the filter.
        """
        kept: list[dict[str, Any]] = []
        violations: list[ConstraintViolation] = []
        for action in actions:
            if not isinstance(action, dict):
                # An unparseable entry is not a constraint violation per se,
                # but it's also not a valid action — drop it with a synthetic
                # violation record so the audit row reflects the filter.
                violations.append(
                    ConstraintViolation(
                        constraint="malformed_action",
                        field="<root>",
                        value=0.0,
                        limit=0.0,
                        detail=f"non-dict action entry: {type(action).__name__}",
                    )
                )
                continue
            v = self.validate_action(action)
            if v is None:
                kept.append(action)
            else:
                violations.append(v)
        return kept, violations


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort coercion. Returns ``None`` for non-numeric input."""
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int``; coercing here ensures
        # ``False`` is checked as 0.0 and cannot bypass SoC validation.
        return float(value)
    if isinstance(value, (int, float)):
        coerced = float(value)
        return coerced if math.isfinite(coerced) else None
    if isinstance(value, str):
        try:
            coerced = float(value)
            return coerced if math.isfinite(coerced) else None
        except ValueError:
            return None
    return None
