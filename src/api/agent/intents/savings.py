"""Deterministic ``savings`` intent for the depot chat agent.

Answers "how much did we save (overnight / this month / last week)?" by
reusing the savings computation that already backs
``GET /depots/{id}/savings-summary`` (:mod:`src.api.savings`). This module
holds only the *pure* pieces the fast-path handler in
``src/api/agent/controller.py`` composes:

1. :func:`resolve_savings_window` — map the user's phrasing to a concrete
   UTC ``[start, end)`` window in the depot's local timezone. "overnight"
   is the depot-local 17:00→07:00 window (DST-aware, via
   :func:`src.api.savings.overnight_window_utc`); the calendar phrases reuse
   :func:`src.api.agent.resolve.resolve_time_window` so the bounds match the
   consumption fast path exactly. No qualifier ⇒ month-to-date.
2. :func:`render_savings_answer` — turn the aggregated euro figures into a
   natural-language reply, mirroring the route's "—" semantics when the
   baseline is unknown (no prices / no bidding zone).

The handler does the I/O (timezone load, per-depot
:func:`src.api.savings.compute_savings_for_window`, aggregation); keeping the
window logic and the wording here makes both unit-testable without a database
or an LLM call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.api.agent.resolve import resolve_relative_bounds
from src.api.savings import overnight_window_utc

# Window kind → human label used in the rendered answer.
_WINDOW_LABELS: dict[str, str] = {
    "overnight": "overnight",
    "month_to_date": "this month so far",
    "today": "today",
    "yesterday": "yesterday",
    "this_week": "this week",
    "last_week": "last week",
    "last_month": "last month",
}

# Ordered (specific → general) phrase patterns. The first match wins; no
# match falls through to month-to-date (the route's default window).
_WINDOW_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("overnight", re.compile(r"\b(overnight|last night|tonight)\b")),
    ("yesterday", re.compile(r"\byesterday\b")),
    ("last_week", re.compile(r"\blast week\b")),
    ("this_week", re.compile(r"\b(this week|this past week)\b")),
    ("last_month", re.compile(r"\blast month\b")),
    (
        "month_to_date",
        re.compile(r"\b(this month|month to date|month-to-date|so far this month)\b"),
    ),
    ("today", re.compile(r"\btoday\b")),
)


@dataclass(frozen=True)
class SavingsWindow:
    """A resolved savings window: UTC bounds + a human label."""

    kind: str
    label: str
    period_start: datetime
    period_end: datetime


def classify_savings_window(message: str) -> str:
    """Return the window kind keyword for a savings message.

    Pure string→string: one of the keys of :data:`_WINDOW_LABELS`. Defaults
    to ``"month_to_date"`` when the message carries no recognised time phrase
    (so a bare "how much have we saved?" reports month-to-date, matching the
    dashboard's savings card).
    """
    text = (message or "").lower()
    for kind, pattern in _WINDOW_PATTERNS:
        if pattern.search(text):
            return kind
    return "month_to_date"


def resolve_savings_window(
    message: str,
    now: datetime,
    tz_name: Optional[str],
) -> SavingsWindow:
    """Resolve a savings message + clock + depot tz to a concrete window.

    Args:
        message: The raw user message.
        now: Current instant (UTC-aware) — the relative anchor.
        tz_name: Depot IANA timezone; ``None`` degrades to UTC (same
            fallback as :func:`src.api.savings.overnight_window_utc`).

    Returns:
        A :class:`SavingsWindow` with UTC ``[period_start, period_end)``.
    """
    kind = classify_savings_window(message)
    label = _WINDOW_LABELS[kind]

    if kind == "overnight":
        start, end = overnight_window_utc(now, tz_name)
        # Clamp the end to `now` like the calendar windows below: asked between
        # local midnight and 07:00 the canonical 17:00→07:00 window ends in the
        # future, so the day-ahead price average would cover not-yet-charged
        # hours and inflate the baseline against past-only sessions. After
        # 07:00 the window already ends <= now, so the clamp is a no-op.
        return SavingsWindow(kind=kind, label=label, period_start=start, period_end=min(end, now))

    if kind == "month_to_date":
        # Month start (depot-local) → now. "this_month" relative gives the
        # full calendar month; we clamp the end to `now` because you cannot
        # have saved in the future.
        start = resolve_relative_bounds("this_month", tz_name, now=now)[0]
        return SavingsWindow(kind=kind, label=label, period_start=start, period_end=now)

    start, end = resolve_relative_bounds(kind, tz_name, now=now)
    # Clamp the end to `now` for every window: "today" / "this week" span
    # future hours whose day-ahead prices already exist and would otherwise
    # inflate the baseline average against past-only sessions. Fully-past
    # windows (yesterday / last week / last month) already end <= now, so
    # the clamp is a no-op there.
    return SavingsWindow(kind=kind, label=label, period_start=start, period_end=min(end, now))


def _fmt_eur(amount: float) -> str:
    """Format a euro amount with a thousands separator and 2 decimals."""
    return f"€{amount:,.2f}"


def render_savings_answer(
    *,
    actual_eur: float,
    baseline_eur: float,
    saved_eur: float,
    saved_pct: float,
    label: str,
    depot_count: int = 1,
    baseline_known: bool = True,
) -> str:
    """Render the aggregated savings figures as a natural-language reply.

    Mirrors the route's degraded-data semantics: when the baseline is
    *unknown* (no day-ahead prices / no bidding zone) we report spend only
    and say the saving is unavailable rather than printing a misleading
    "0% saved". This is driven by the explicit ``baseline_known`` flag, not
    inferred from ``baseline_eur == 0`` — after multi-depot aggregation a
    genuine baseline can sum to 0 (negative prices cancelling), which must
    NOT be mistaken for "no price data".

    Args:
        actual_eur: What the depot(s) actually spent charging in the window.
        baseline_eur: Estimated flat-rate (unmanaged) cost over the window.
        saved_eur: ``baseline_eur - actual_eur`` (negative ⇒ did worse).
        saved_pct: Signed percentage saved vs the baseline magnitude.
        label: Human window label (e.g. "overnight", "this month so far").
        depot_count: Number of depots aggregated (annotated when > 1).
        baseline_known: False when no contributing depot had a priceable
            baseline (no zone / no prices / no energy) — report spend only.

    Returns:
        A single-sentence operator-facing answer.
    """
    scope = "" if depot_count <= 1 else f" across {depot_count} depots"

    # Unknown baseline → report spend only (do not infer from baseline_eur==0).
    if not baseline_known:
        if actual_eur == 0:
            return (
                f"No charging was recorded {label}{scope}, so there was nothing " "spent or saved."
            )
        return (
            f"You spent {_fmt_eur(actual_eur)} charging {label}{scope}. I don't "
            "have day-ahead price data for that window, so I can't estimate the "
            "savings versus an unmanaged baseline."
        )

    if saved_eur >= 0:
        return (
            f"You saved about {_fmt_eur(saved_eur)} ({saved_pct:.1f}%) charging "
            f"{label}{scope}: {_fmt_eur(actual_eur)} spent versus an estimated "
            f"{_fmt_eur(baseline_eur)} at flat day-ahead prices."
        )
    return (
        f"Charging {label}{scope} cost about {_fmt_eur(-saved_eur)} "
        f"({abs(saved_pct):.1f}%) more than the flat day-ahead baseline: "
        f"{_fmt_eur(actual_eur)} spent versus an estimated {_fmt_eur(baseline_eur)}."
    )
