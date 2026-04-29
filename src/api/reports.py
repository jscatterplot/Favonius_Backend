"""Energy reporting aggregation helpers.

Pure-Python aggregation over ``charging_sessions`` rows. The fetch layer
(see :mod:`src.api.main`) returns minimal session rows and this module
buckets them in depot-local time and computes per-bucket cost.

Aggregation source of truth is ``charging_sessions.energy_delivered_kwh``;
raw telemetry deltas are intentionally not used.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Literal, Optional
from zoneinfo import ZoneInfo

GroupBy = Literal["vehicle", "charger", "driver", "card"]

_GROUP_BY_VALUES: tuple[str, ...] = ("vehicle", "charger", "driver", "card")

_UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class SessionRow:
    """Minimal projection of a charging_sessions row used by aggregation."""

    start_time: datetime
    end_time: Optional[datetime]
    energy_kwh: Optional[float]
    cost_total: Optional[float]
    vehicle_id: Optional[str]
    charger_id: Optional[str]
    driver_id: Optional[str]
    card_id: Optional[str]


def _bucket_for(start_time: datetime, tz: ZoneInfo) -> str:
    """Return the YYYY-MM bucket key in the depot-local timezone."""
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=ZoneInfo("UTC"))
    local = start_time.astimezone(tz)
    return f"{local.year:04d}-{local.month:02d}"


def _group_value(row: SessionRow, group_by: Optional[str]) -> Optional[str]:
    """Return the grouping value for ``row`` (None when no grouping)."""
    if group_by is None:
        return None
    if group_by == "vehicle":
        return row.vehicle_id
    if group_by == "charger":
        return row.charger_id
    if group_by == "driver":
        return row.driver_id
    if group_by == "card":
        return row.card_id
    raise ValueError(f"Unsupported group_by: {group_by!r}")


def _apply_group_id(row_payload: dict, group_by: Optional[str], value: Optional[str]) -> None:
    """Set the grouping id field on ``row_payload`` (NULL → 'unassigned')."""
    if group_by is None:
        return
    field = f"{group_by}_id"
    row_payload[field] = value if value is not None else _UNASSIGNED


def aggregate_energy_rows(
    sessions: Iterable[SessionRow],
    *,
    timezone: str,
    group_by: Optional[str],
    under_cap_rate: Optional[float],
    currency: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """Bucket sessions into monthly rows in depot-local TZ.

    ``from_date`` and ``to_date`` are inclusive on the calendar-day boundary
    in the depot timezone. Sessions whose ``start_time`` falls outside that
    window are filtered out.

    Cost rule: when every session in a bucket has a populated ``cost_total``
    we sum them and report ``estimated=false``. When any session is missing
    ``cost_total`` we fall back to ``energy_kwh × under_cap_rate`` for the
    missing portion and flag the row ``estimated=true``. If the rate is not
    configured the missing portion contributes zero and the row is still
    flagged as estimated.
    """
    if group_by is not None and group_by not in _GROUP_BY_VALUES:
        raise ValueError(f"Unsupported group_by: {group_by!r}")

    tz = ZoneInfo(timezone)
    window_start_local = datetime.combine(from_date, datetime.min.time())
    window_end_local = datetime.combine(to_date + timedelta(days=1), datetime.min.time())
    window_start_utc = window_start_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
    window_end_utc = window_end_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))

    buckets: dict[tuple[str, Optional[str]], dict[str, float]] = {}

    for row in sessions:
        start = row.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo("UTC"))
        if start < window_start_utc or start >= window_end_utc:
            continue

        bucket = _bucket_for(start, tz)
        group_val = _group_value(row, group_by)
        key = (bucket, group_val)
        agg = buckets.setdefault(
            key,
            {
                "energy_kwh": 0.0,
                "session_count": 0,
                "duration_hours": 0.0,
                "cost_total_sum": 0.0,
                "cost_missing_count": 0,
                "energy_kwh_missing_cost": 0.0,
            },
        )

        energy = float(row.energy_kwh) if row.energy_kwh is not None else 0.0
        agg["energy_kwh"] += energy
        agg["session_count"] += 1
        if row.end_time is not None:
            duration_h = max((row.end_time - row.start_time).total_seconds() / 3600.0, 0.0)
            agg["duration_hours"] += duration_h
        if row.cost_total is None:
            agg["cost_missing_count"] += 1
            agg["energy_kwh_missing_cost"] += energy
        else:
            agg["cost_total_sum"] += float(row.cost_total)

    rows: list[dict] = []
    for (bucket, group_val), agg in sorted(
        buckets.items(),
        key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or ""),
    ):
        estimated = agg["cost_missing_count"] > 0
        if estimated:
            estimate = (
                agg["energy_kwh_missing_cost"] * under_cap_rate
                if under_cap_rate is not None
                else 0.0
            )
            cost_amount = agg["cost_total_sum"] + estimate
        else:
            cost_amount = agg["cost_total_sum"]

        avg_kw = (
            agg["energy_kwh"] / agg["duration_hours"]
            if agg["duration_hours"] > 0
            else 0.0
        )

        row_payload: dict = {
            "bucket": bucket,
            "energy_kwh": round(agg["energy_kwh"], 6),
            "session_count": int(agg["session_count"]),
            "avg_kw": round(avg_kw, 6),
            "cost": {
                "amount": round(cost_amount, 6),
                "currency": currency,
                "estimated": estimated,
            },
        }
        _apply_group_id(row_payload, group_by, group_val)
        rows.append(row_payload)
    return rows


_CSV_BASE_COLUMNS = (
    "bucket",
    "energy_kwh",
    "session_count",
    "avg_kw",
    "cost_amount",
    "cost_currency",
    "cost_estimated",
)


def csv_columns(group_by: Optional[str]) -> tuple[str, ...]:
    """Return the ordered CSV column names for a given grouping."""
    if group_by is None:
        return _CSV_BASE_COLUMNS
    group_field = f"{group_by}_id"
    return ("bucket", group_field, *_CSV_BASE_COLUMNS[1:])


def stream_rows_as_csv(
    rows: list[dict],
    *,
    group_by: Optional[str],
) -> Iterator[str]:
    """Yield CSV chunks for ``rows`` in the same order as the JSON response.

    Each row in the JSON body produces exactly one CSV record (header line
    included once at the top). Columns mirror the JSON shape but flatten
    ``cost`` into three columns: ``cost_amount``, ``cost_currency``,
    ``cost_estimated``.
    """
    columns = csv_columns(group_by)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        cost = row["cost"]
        record = [
            row["bucket"],
            *(
                [row.get(f"{group_by}_id", "")]
                if group_by is not None
                else []
            ),
            row["energy_kwh"],
            row["session_count"],
            row["avg_kw"],
            cost["amount"],
            cost["currency"],
            "true" if cost["estimated"] else "false",
        ]
        writer.writerow(record)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
