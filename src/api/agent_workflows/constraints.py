"""Hard-constraint guard for workflow outputs (PRD §10.3).

The depot agent shares its constraints layer with the optimiser. Two
constraints are enforceable purely from an agent's structured output
without round-tripping through the MILP model — and these are the
ones the guard checks here:

- ``vehicle.departure_soc`` must be ≥ 99% of the scheduled target SoC.
- A proposed grid-power setpoint must not exceed the depot's
  ``max_grid_kw``.

The guard runs once, at the very end of a turn, on the structured
output object the agent emits via the reserved ``submit_decision`` tool.
Each proposed action is inspected; offenders are stripped from the
``proposed_actions`` array and recorded in
:attr:`Decision.constraint_violations` so the audit trail makes the
rejection visible.

The other constraints in PRD §10.3 (solve time, OCPP version, connector
type, HoS, SLAs) are enforced upstream — solve time inside the optimiser,
the rest inside the data layer the agent reads. They do not appear here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


# PRD §10.3 row 1. Encoded as a constant so the test asserting the
# threshold can import it rather than duplicating the literal.
DEPARTURE_SOC_FLOOR: float = 0.99

# The structured output schema the guard understands. Documented here
# in one place so workflow authors writing prompts know what shape to
# emit. The runtime's reserved ``submit_decision`` tool advertises the
# same shape to the model.
PROPOSED_ACTION_KINDS = (
    "set_departure_soc",  # {"vehicle_id": str, "target_soc": float in 0..1}
    "set_grid_power_kw",  # {"value_kw": float}
    "set_charger_power_kw",  # {"charger_id": str, "value_kw": float}
    "reassign_to_route",  # {"vehicle_id": str, "route_id": str}
    "draft_email",  # {"to": str, "subject": str, "body": str}
    "draft_vendor_ticket",  # {"vendor": str, "body": str}
    "notify_manager",  # {"summary": str}
)


@dataclass(frozen=True)
class DepotConstraints:
    """The substrate-supplied numbers the guard checks against.

    Loaded once at the start of a turn from
    :class:`~src.core.models.DepotConfig` (or an equivalent fixture in
    tests). Frozen because the values must not drift mid-turn.
    """

    depot_id: UUID
    max_grid_kw: float
    departure_soc_floor: float = DEPARTURE_SOC_FLOOR


class HardConstraintGuard:
    """Filters constraint-violating actions out of an agent's output.

    The guard is stateless — it consumes one ``output`` dict plus a
    :class:`DepotConstraints` and returns a sanitised copy plus a list
    of human-readable violation messages. Stateless so it can be reused
    across concurrent turns without locking.
    """

    def filter_output(
        self, output: dict[str, Any], constraints: DepotConstraints
    ) -> tuple[dict[str, Any], list[str]]:
        """Return ``(filtered_output, violations)``.

        The agent's ``output`` schema is::

            {
              "summary": str,
              "proposed_actions": list[dict],
              ...                                # free-form keys allowed
            }

        Every action whose type is in :data:`PROPOSED_ACTION_KINDS` is
        checked. Actions of unknown type are passed through untouched —
        new action shapes are added by extending the guard, not by
        opt-out.
        """
        actions = output.get("proposed_actions") or []
        if not isinstance(actions, list):
            # Defensive: the model may have produced a malformed payload.
            # Treat anything non-listy as "no actions" and flag it.
            return (
                {**output, "proposed_actions": []},
                [
                    "proposed_actions was not a list; ignored entirely "
                    "(model produced malformed output)"
                ],
            )

        kept: list[dict[str, Any]] = []
        violations: list[str] = []
        for action in actions:
            if not isinstance(action, dict):
                violations.append("skipped non-dict entry in proposed_actions")
                continue
            verdict = self._check_action(action, constraints)
            if verdict is None:
                kept.append(action)
            else:
                violations.append(verdict)

        if violations:
            logger.warning(
                "HardConstraintGuard rejected %d action(s) for depot %s: %s",
                len(violations),
                constraints.depot_id,
                violations,
            )

        # Avoid mutating the caller's dict.
        filtered = dict(output)
        filtered["proposed_actions"] = kept
        return filtered, violations

    # ── Per-action checks ────────────────────────────────────────────────
    def _check_action(self, action: dict[str, Any], constraints: DepotConstraints) -> str | None:
        """Return ``None`` if the action is permitted, else a reason string."""
        kind = action.get("type")
        if kind == "set_departure_soc":
            target = action.get("target_soc")
            if not isinstance(target, (int, float)):
                return "set_departure_soc: target_soc missing or non-numeric"
            if target < constraints.departure_soc_floor:
                return (
                    f"set_departure_soc: target_soc={target:.3f} violates "
                    f"departure SoC floor {constraints.departure_soc_floor:.2f} "
                    "(PRD §10.3)"
                )
            return None

        if kind == "set_grid_power_kw":
            value = action.get("value_kw")
            if not isinstance(value, (int, float)):
                return "set_grid_power_kw: value_kw missing or non-numeric"
            if value > constraints.max_grid_kw:
                return (
                    f"set_grid_power_kw: value_kw={value:.2f} exceeds depot "
                    f"max_grid_kw={constraints.max_grid_kw:.2f} (PRD §10.3)"
                )
            return None

        if kind == "set_charger_power_kw":
            value = action.get("value_kw")
            if not isinstance(value, (int, float)):
                return "set_charger_power_kw: value_kw missing or non-numeric"
            # A single charger cannot, alone, push the site over its grid
            # cap (other loads are zero in that limit), so the guard
            # checks individual charger setpoints against the same cap.
            if value > constraints.max_grid_kw:
                return (
                    f"set_charger_power_kw: value_kw={value:.2f} exceeds "
                    f"depot max_grid_kw={constraints.max_grid_kw:.2f} "
                    "(PRD §10.3)"
                )
            return None

        # Unknown / non-constraint-relevant actions pass through.
        return None


__all__ = [
    "DEPARTURE_SOC_FLOOR",
    "DepotConstraints",
    "HardConstraintGuard",
    "PROPOSED_ACTION_KINDS",
]
