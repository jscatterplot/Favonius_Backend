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
from typing import Iterable, Iterator, Optional
from zoneinfo import ZoneInfo

REPORT_GROUP_BY_VALUES: tuple[str, ...] = ("vehicle", "charger", "driver", "card")

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
    card_label: Optional[str] = None
    card_id_tag: Optional[str] = None


def _card_display_key(row: SessionRow) -> Optional[str]:
    """Return the merge key for a card-grouped row.

    Cards sharing a ``label`` (the human display name) collapse into one
    bucket so the report doesn't split the same logical user across the
    multiple physical tags they carry. Falls back to ``id_tag`` when no
    label is set, then to ``card_id`` so legacy rows without enrichment
    keep their pre-merge behaviour.
    """
    if row.card_id is None:
        return None
    return row.card_label or row.card_id_tag or row.card_id


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
        return _card_display_key(row)
    raise ValueError(f"Unsupported group_by: {group_by!r}")


def _apply_group_id(
    row_payload: dict,
    group_by: Optional[str],
    value: Optional[str],
    representative: Optional[SessionRow] = None,
) -> None:
    """Set the grouping id field on ``row_payload`` (NULL → 'unassigned').

    For card grouping the merge key is the label (when present), so the
    output also carries the underlying ``card_id`` and ``card_label``
    from a representative session so the frontend can render the human
    name without an extra lookup.
    """
    if group_by is None:
        return
    field = f"{group_by}_id"
    row_payload[field] = value if value is not None else _UNASSIGNED
    if group_by == "card":
        if representative is not None and representative.card_id is not None:
            row_payload["card_id"] = representative.card_id
            row_payload["card_label"] = (
                representative.card_label
                or representative.card_id_tag
                or representative.card_id
            )
        else:
            row_payload["card_label"] = _UNASSIGNED if value is None else value


def _window_utc_bounds(
    timezone: str, from_date: date, to_date: date
) -> tuple[datetime, datetime]:
    """Compute the half-open UTC window for the depot-local calendar range."""
    tz = ZoneInfo(timezone)
    window_start_local = datetime.combine(from_date, datetime.min.time())
    window_end_local = datetime.combine(to_date + timedelta(days=1), datetime.min.time())
    return (
        window_start_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")),
        window_end_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")),
    )


def _session_in_window(
    row: SessionRow, start_utc: datetime, end_utc: datetime
) -> bool:
    """Return True when ``row.start_time`` is inside the half-open UTC window."""
    start = row.start_time
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("UTC"))
    return start_utc <= start < end_utc


def _new_agg() -> dict[str, float]:
    """Return a zeroed accumulator dict for energy/cost stats."""
    return {
        "energy_kwh": 0.0,
        "energy_kwh_with_duration": 0.0,
        "session_count": 0,
        "duration_hours": 0.0,
        "cost_total_sum": 0.0,
        "cost_missing_count": 0,
        "energy_kwh_missing_cost": 0.0,
    }


def _accumulate(agg: dict[str, float], row: SessionRow) -> None:
    """Add ``row`` into the accumulator in-place."""
    energy = float(row.energy_kwh) if row.energy_kwh is not None else 0.0
    agg["energy_kwh"] += energy
    agg["session_count"] += 1
    if row.end_time is not None:
        duration_h = max((row.end_time - row.start_time).total_seconds() / 3600.0, 0.0)
        agg["duration_hours"] += duration_h
        agg["energy_kwh_with_duration"] += energy
    if row.cost_total is None:
        agg["cost_missing_count"] += 1
        agg["energy_kwh_missing_cost"] += energy
    else:
        agg["cost_total_sum"] += float(row.cost_total)


def _finalize_agg(
    agg: dict[str, float], under_cap_rate: Optional[float], currency: str
) -> dict:
    """Project an accumulator into the report payload (excluding ``bucket``)."""
    estimated = agg["cost_missing_count"] > 0
    if estimated:
        cost_amount = agg["cost_total_sum"] + (
            agg["energy_kwh_missing_cost"] * under_cap_rate
            if under_cap_rate is not None
            else 0.0
        )
    else:
        cost_amount = agg["cost_total_sum"]
    avg_kw = (
        agg["energy_kwh_with_duration"] / agg["duration_hours"]
        if agg["duration_hours"] > 0
        else 0.0
    )
    return {
        "energy_kwh": round(agg["energy_kwh"], 6),
        "session_count": int(agg["session_count"]),
        "avg_kw": round(avg_kw, 6),
        "cost": {
            "amount": round(cost_amount, 6),
            "currency": currency,
            "estimated": estimated,
        },
    }


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

    For ``group_by='card'`` the merge key is the card label (with id_tag
    fallback for unlabelled cards) so two physical tags sharing one
    display name collapse into a single row.
    """
    if group_by is not None and group_by not in REPORT_GROUP_BY_VALUES:
        raise ValueError(f"Unsupported group_by: {group_by!r}")

    tz = ZoneInfo(timezone)
    window_start_utc, window_end_utc = _window_utc_bounds(timezone, from_date, to_date)

    buckets: dict[tuple[str, Optional[str]], dict[str, float]] = {}
    representatives: dict[tuple[str, Optional[str]], SessionRow] = {}

    for row in sessions:
        if not _session_in_window(row, window_start_utc, window_end_utc):
            continue

        bucket = _bucket_for(row.start_time, tz)
        group_val = _group_value(row, group_by)
        key = (bucket, group_val)
        agg = buckets.setdefault(key, _new_agg())
        if group_by == "card" and key not in representatives and row.card_id is not None:
            representatives[key] = row
        _accumulate(agg, row)

    rows: list[dict] = []
    for (bucket, group_val), agg in sorted(
        buckets.items(),
        key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or ""),
    ):
        row_payload: dict = {"bucket": bucket, **_finalize_agg(agg, under_cap_rate, currency)}
        _apply_group_id(
            row_payload,
            group_by,
            group_val,
            representative=representatives.get((bucket, group_val)),
        )
        rows.append(row_payload)
    return rows


def compute_energy_totals(
    sessions: Iterable[SessionRow],
    *,
    timezone: str,
    under_cap_rate: Optional[float],
    currency: str,
    from_date: date,
    to_date: date,
) -> dict:
    """Aggregate all sessions in the window into a single totals row.

    Shares the same per-session accumulator and cost-estimation rule as
    :func:`aggregate_energy_rows` so the two stay in lockstep.
    """
    window_start_utc, window_end_utc = _window_utc_bounds(timezone, from_date, to_date)
    agg = _new_agg()
    for row in sessions:
        if not _session_in_window(row, window_start_utc, window_end_utc):
            continue
        _accumulate(agg, row)
    return _finalize_agg(agg, under_cap_rate, currency)


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
    """Return the ordered CSV column names for a given grouping.

    Card grouping adds a ``card_label`` column after ``card_id`` so the
    human-readable name travels alongside the UUID.
    """
    if group_by is None:
        return _CSV_BASE_COLUMNS
    group_field = f"{group_by}_id"
    if group_by == "card":
        return ("bucket", group_field, "card_label", *_CSV_BASE_COLUMNS[1:])
    return ("bucket", group_field, *_CSV_BASE_COLUMNS[1:])


def stream_rows_as_csv(
    rows: list[dict],
    *,
    group_by: Optional[str],
    totals: Optional[dict] = None,
) -> Iterator[str]:
    """Yield CSV chunks for ``rows`` in the same order as the JSON response.

    Each row in the JSON body produces exactly one CSV record (header line
    included once at the top). Columns mirror the JSON shape but flatten
    ``cost`` into three columns: ``cost_amount``, ``cost_currency``,
    ``cost_estimated``.

    When ``totals`` is provided, a final ``TOTAL`` row is emitted with
    the cross-bucket sums in the energy / count / cost columns and empty
    cells in the bucket / grouping / avg_kw columns.
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
        if group_by == "card":
            group_cells = [
                row.get("card_id", ""),
                row.get("card_label", ""),
            ]
        elif group_by is not None:
            group_cells = [row.get(f"{group_by}_id", "")]
        else:
            group_cells = []
        record = [
            row["bucket"],
            *group_cells,
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

    if totals is not None:
        totals_cost = totals.get("cost") or {}
        if group_by == "card":
            group_cells = ["", ""]
        elif group_by is not None:
            group_cells = [""]
        else:
            group_cells = []
        record = [
            "TOTAL",
            *group_cells,
            totals.get("energy_kwh", 0.0),
            totals.get("session_count", 0),
            "",
            totals_cost.get("amount", 0.0),
            totals_cost.get("currency", ""),
            "true" if totals_cost.get("estimated") else "false",
        ]
        writer.writerow(record)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
