"""Protocol shared by every intent compiler.

A compiler is a pure function: ``QueryPlan + resolved entities +
resolved time window → (sql, params)``. No DB I/O, no LLM, no
side effects. The router executes the returned SQL with the returned
parameters against the time-series pool.
"""

from __future__ import annotations

from typing import Any, Protocol

from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow
from src.api.agent.plan import QueryPlan


class IntentCompiler(Protocol):
    """Contract every per-intent compiler implements.

    The returned ``params`` list is positional and matches the
    ``$1, $2, …`` placeholders in the SQL string in order. Compilers
    raise :class:`ValueError` when the plan + resolved set does not
    contain enough information to build a query (for example, a
    consumption-by-user query with no resolved drivers).
    """

    def compile(
        self,
        plan: QueryPlan,
        resolved: list[ResolvedEntity],
        window: ResolvedTimeWindow,
    ) -> tuple[str, list[Any]]:
        """Compile ``plan`` into a parameterized SQL string + params."""
        ...
