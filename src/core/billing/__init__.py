"""Billing calculations on top of charging session data.

Public entry points:
    * :class:`SessionCostResult` — tagged-union return type.
    * :func:`compute_session_cost` — strategy-dispatching calculator.
    * :func:`write_session_cost` — idempotent UPDATE on charging_sessions.
"""

from .session_cost import (
    SessionCostResult,
    SessionCostSource,
    compute_session_cost,
    write_session_cost,
)

__all__ = [
    "SessionCostResult",
    "SessionCostSource",
    "compute_session_cost",
    "write_session_cost",
]
