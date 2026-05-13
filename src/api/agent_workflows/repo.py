"""Decision repository — thin abstraction over Sprint 1's writer.

Sprint 1 (`src/api/agent_workflows/repository.py`) is the canonical
data layer: a free-function ``insert_decision(pool, decision)`` against
the append-only ``decisions`` hypertable (migration 037, PRD §10.4).
The runtime depends on the *behaviour* — write exactly one row per
turn — not on the concrete writer, so this module exposes a Protocol
plus two implementations:

* :class:`AsyncpgDecisionRepo` — wraps Sprint 1's ``insert_decision``
  with an asyncpg pool. The production wiring.
* :class:`InMemoryDecisionRepo` — collects decisions in a list. Used
  in unit tests and dry runs.

Keeping the Protocol means the runtime stays trivially testable without
spinning up a database, while the production path is a one-line
adapter onto the canonical writer.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from src.api.agent_workflows.models import Decision


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


class AsyncpgDecisionRepo:
    """Production adapter: delegates to ``repository.insert_decision``.

    Kept as a thin object (rather than a bare callable) so the
    constructor signature can grow later (retries, tracing, batched
    writes) without changing the runtime's dependency.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def write(self, decision: Decision) -> None:
        # Local import to keep the runtime import graph free of asyncpg
        # when tests run the in-memory path.
        from src.api.agent_workflows.repository import insert_decision

        await insert_decision(self._pool, decision)
