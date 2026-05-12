"""Decision repository protocol.

The runtime writes exactly one :class:`~src.api.agent_workflows.schemas.Decision`
row at the end of each turn. The repo is abstracted behind a Protocol so:

- Unit tests inject :class:`InMemoryDecisionRepo` and inspect what was
  written, without touching a database.
- The production wiring (a thin asyncpg writer against the ``decisions``
  table that Sprint 1 owns the migration for) lands in a later sprint
  without changing the runtime contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.api.agent_workflows.schemas import Decision


@runtime_checkable
class DecisionRepo(Protocol):
    """Minimum repo contract the runtime depends on."""

    async def write(self, decision: Decision) -> None:
        """Persist one decision. Must be idempotent on ``decision.id``."""
        ...


class InMemoryDecisionRepo:
    """Captures decisions in-process. Useful in tests and dry runs."""

    def __init__(self) -> None:
        self.decisions: list[Decision] = []

    async def write(self, decision: Decision) -> None:
        self.decisions.append(decision)

    # Convenience for tests.
    @property
    def last(self) -> Decision:
        if not self.decisions:
            raise IndexError("InMemoryDecisionRepo has no decisions")
        return self.decisions[-1]
