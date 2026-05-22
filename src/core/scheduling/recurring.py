"""Recurring schedule template expansion.

Pure functions: ``(templates, cancellations, depot_tz, horizon_start, horizon_end)``
``→ list[trip_dict]`` with the same shape that ``StateAssembler._get_schedules``
returns from the ``schedules`` table. No DB access. All edge cases (DST,
cancellations, manual-vs-recurring tiebreaker) are exercised by
``tests/unit/test_recurring_schedule_expansion.py``.

DST handling
------------
We materialise occurrences in depot-local wall-clock, then convert to UTC via
the IANA zoneinfo database. This produces the expected 23h / 25h days
automatically. Two boundary cases need explicit policy:

* **Spring-forward (nonexistent local hour).** When the configured
  ``HH:MM`` would fall in the skipped range we shift forward minute-by-minute
  to the next valid local time and emit a warning.
* **Fall-back (ambiguous local hour).** We deterministically choose the
  first (pre-shift) occurrence by passing ``fold=0`` to ``datetime``.

Manual-vs-recurring tiebreaker
------------------------------
Spec: "one-off /schedule/manual entries override recurring trips on the
same date when both exist for the same vehicle (later-created wins)."

Tiebreaker is evaluated per ``(vehicle_id, depot_local_date)``:

* If only manual rows exist for that day → use them.
* If only recurring would yield trips → emit the recurring trip.
* If both exist → compare ``created_at`` of the template against the latest
  ``created_at`` among the manual rows landing on that day. Whichever is
  newer wins; the loser's rows for that day are dropped.

The merge happens after expansion so the recurring code stays pure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DAYS_OF_WEEK: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX: dict[str, int] = {name: idx for idx, name in enumerate(DAYS_OF_WEEK)}


@dataclass(frozen=True)
class RecurringTemplate:
    """A daily-repeating schedule pattern for one vehicle on one route."""

    template_id: UUID
    depot_id: UUID
    vehicle_id: UUID
    route_id: str
    departure_time_of_day: time
    return_time_of_day: time
    days_of_week: tuple[str, ...]
    start_date: date
    end_date: Optional[date]
    required_soc: float
    energy_kwh: Optional[float]
    active: bool
    created_at: datetime

    @property
    def crosses_midnight(self) -> bool:
        return self.return_time_of_day <= self.departure_time_of_day


@dataclass(frozen=True)
class ScheduleCancellation:
    """An operator-cancelled single occurrence of a recurring template."""

    template_id: UUID
    occurrence_date: date


@dataclass(frozen=True)
class ManualScheduleRow:
    """Manual ``schedules`` row used for the tiebreaker against templates.

    Only the fields the merge logic reads. Full row shape is whatever
    ``StateAssembler._get_schedules`` returns; that dict is passed through
    untouched in the output.
    """

    vehicle_id: UUID
    departure_time_utc: datetime
    created_at: datetime
    payload: dict = field(default_factory=dict)


# ── Public API ──────────────────────────────────────────────────────────────


def expand_recurring_templates(
    templates: Iterable[RecurringTemplate],
    cancellations: Iterable[ScheduleCancellation],
    *,
    depot_tz: ZoneInfo,
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[dict]:
    """Materialize templates into concrete trip dicts within ``[start, end)``.

    Args:
        templates: All ``active=True`` templates for the depot.
        cancellations: All cancellations whose occurrence_date might land in
            the horizon. ``(template_id, occurrence_date)`` pairs in this
            iterable are skipped.
        depot_tz: IANA timezone the depot operates in (e.g. ``Europe/Vilnius``).
        horizon_start: Inclusive UTC start of the optimization horizon.
        horizon_end: Exclusive UTC end of the optimization horizon.

    Returns:
        List of trip dicts compatible with ``schedules`` rows:
        ``{vehicle_id, departure_time, return_time, estimated_energy_kwh,
        route_id, _source, _template_id, _created_at}``. The underscore-
        prefixed keys are internal metadata used by ``merge_recurring_with_manual``
        for the tiebreaker; downstream code in ``StateAssembler`` and the
        snapshot serializer is expected to ignore them (the existing
        snapshot serializer already does — it only persists known keys).
    """
    if horizon_start.tzinfo is None or horizon_end.tzinfo is None:
        raise ValueError("horizon_start/horizon_end must be timezone-aware")

    if horizon_end <= horizon_start:
        return []

    cancelled: set[tuple[UUID, date]] = {
        (c.template_id, c.occurrence_date) for c in cancellations
    }

    # Iterate one depot-local day at a time. A trip can cross midnight, so we
    # need to consider the day before horizon_start too (its return may land
    # inside the horizon). We also consider the day after horizon_end is
    # already-finished UTC-side but might still depart before horizon_end in
    # local time — bound on the local-date side and emit anything whose
    # *departure* falls within [horizon_start, horizon_end).
    local_start = horizon_start.astimezone(depot_tz)
    local_end = horizon_end.astimezone(depot_tz)
    # Walk from one local-day before the horizon (to catch crosses-midnight
    # trips whose return lands inside) up to and including the local end day.
    walk_date = (local_start.date() - timedelta(days=1))
    last_date = local_end.date()

    out: list[dict] = []
    while walk_date <= last_date:
        weekday_name = DAYS_OF_WEEK[walk_date.weekday()]
        for tpl in templates:
            if not tpl.active:
                continue
            if weekday_name not in tpl.days_of_week:
                continue
            if walk_date < tpl.start_date:
                continue
            if tpl.end_date is not None and walk_date > tpl.end_date:
                continue
            if (tpl.template_id, walk_date) in cancelled:
                continue

            departure_utc = _to_utc(walk_date, tpl.departure_time_of_day, depot_tz)
            return_date = walk_date + timedelta(days=1) if tpl.crosses_midnight else walk_date
            return_utc = _to_utc(return_date, tpl.return_time_of_day, depot_tz)

            # We index by *departure* falling inside the horizon, matching
            # the existing manual-schedule query in StateAssembler (which
            # filters on departure_time only).
            if not (horizon_start <= departure_utc < horizon_end):
                continue

            out.append(
                {
                    "vehicle_id": str(tpl.vehicle_id),
                    "departure_time": departure_utc,
                    "return_time": return_utc,
                    "estimated_energy_kwh": tpl.energy_kwh,
                    "route_id": tpl.route_id,
                    "_source": "recurring",
                    "_template_id": tpl.template_id,
                    "_created_at": tpl.created_at,
                    "_occurrence_date": walk_date,
                }
            )
        walk_date += timedelta(days=1)
    return out


def merge_recurring_with_manual(
    manual_rows: list[dict],
    recurring_rows: list[dict],
    *,
    depot_tz: ZoneInfo,
) -> list[dict]:
    """Apply the per-(vehicle, depot_local_date) tiebreaker.

    ``manual_rows`` are dicts as returned from the ``schedules`` SELECT —
    they must carry ``vehicle_id``, ``departure_time`` (UTC tz-aware), and
    ``created_at`` (used only for the tiebreaker). ``recurring_rows`` are
    the output of :func:`expand_recurring_templates`.

    Conflict resolution per ``(vehicle_id, depot_local_date)``:
      * Both present → compare the max ``manual.created_at`` against the
        recurring rows' ``_created_at``. Newer wins; loser's rows on that
        day are dropped.
      * Only one present → keep it.

    Output rows are cleaned: the merge function strips ``created_at`` from
    manual rows and the internal ``_*`` metadata from recurring rows so the
    returned shape matches the original ``_get_schedules`` contract
    (``vehicle_id``, ``departure_time``, ``return_time``, ``estimated_energy_kwh``,
    ``route_id``).
    """
    # Bucket both sides by (vehicle_id, local_date).
    by_key: dict[tuple[str, date], dict[str, list[dict]]] = {}

    def _ensure(key: tuple[str, date]) -> dict[str, list[dict]]:
        bucket = by_key.get(key)
        if bucket is None:
            bucket = {"manual": [], "recurring": []}
            by_key[key] = bucket
        return bucket

    for row in manual_rows:
        local_date = row["departure_time"].astimezone(depot_tz).date()
        key = (str(row["vehicle_id"]), local_date)
        _ensure(key)["manual"].append(row)

    for row in recurring_rows:
        key = (str(row["vehicle_id"]), row["_occurrence_date"])
        _ensure(key)["recurring"].append(row)

    merged: list[dict] = []
    for _key, bucket in by_key.items():
        manual = bucket["manual"]
        recurring = bucket["recurring"]
        if manual and recurring:
            manual_latest = max(r["created_at"] for r in manual)
            recurring_latest = max(r["_created_at"] for r in recurring)
            if manual_latest >= recurring_latest:
                merged.extend(_strip_internal_keys(r) for r in manual)
            else:
                merged.extend(_strip_internal_keys(r) for r in recurring)
        elif manual:
            merged.extend(_strip_internal_keys(r) for r in manual)
        else:
            merged.extend(_strip_internal_keys(r) for r in recurring)

    merged.sort(key=lambda r: r["departure_time"])
    return merged


# ── Helpers ─────────────────────────────────────────────────────────────────


_INTERNAL_KEYS = {"created_at"}


def _strip_internal_keys(row: dict) -> dict:
    return {
        k: v for k, v in row.items()
        if not k.startswith("_") and k not in _INTERNAL_KEYS
    }


def _to_utc(local_date: date, local_time: time, tz: ZoneInfo) -> datetime:
    """Localize a (date, time) to ``tz`` and return UTC.

    Handles DST edges:
      * Nonexistent (spring-forward) → step forward by 1 minute until valid.
      * Ambiguous (fall-back) → ``fold=0`` selects the first occurrence
        deterministically.
    """
    naive = datetime.combine(local_date, local_time)
    candidate = naive.replace(tzinfo=tz, fold=0)
    # zoneinfo silently maps nonexistent times by treating them as if DST
    # were already in effect, producing a UTC offset that's "off by one
    # hour" relative to the same wall-clock minute on adjacent days. Detect
    # by round-tripping: convert to UTC and back; if the wall-clock minute
    # changes, the local time we asked for didn't exist.
    if _is_nonexistent(naive, tz):
        # Step forward minute by minute (caps at 120 to avoid an infinite
        # loop on a pathological tzdata entry — real DST jumps are 60 min).
        stepped = naive
        for _ in range(120):
            stepped += timedelta(minutes=1)
            if not _is_nonexistent(stepped, tz):
                logger.warning(
                    "Recurring trip wall-clock %s did not exist in %s on %s; "
                    "shifted forward to %s.",
                    local_time.isoformat(timespec="minutes"),
                    tz.key,
                    local_date.isoformat(),
                    stepped.time().isoformat(timespec="minutes"),
                )
                candidate = stepped.replace(tzinfo=tz, fold=0)
                break
        else:  # pragma: no cover - defensive
            logger.error(
                "Could not resolve nonexistent local time %s on %s in %s",
                local_time, local_date, tz.key,
            )
    return candidate.astimezone(ZoneInfo("UTC"))


def _is_nonexistent(naive: datetime, tz: ZoneInfo) -> bool:
    """True if ``naive`` falls in a spring-forward gap for ``tz``.

    A nonexistent local time round-trips to a different wall-clock minute:
    naive→local-aware (fold=0) → UTC → local gives back the post-shift
    time, not the original.
    """
    aware = naive.replace(tzinfo=tz, fold=0)
    round_tripped = aware.astimezone(ZoneInfo("UTC")).astimezone(tz)
    return (
        round_tripped.hour != naive.hour
        or round_tripped.minute != naive.minute
        or round_tripped.date() != naive.date()
    )
