"""Pure scheduling + value-mapping helpers for report schedules.

Deliberately free of any third-party imports (no asyncpg / fastapi / pydantic)
so the DST-sensitive timing math and payload validation can be unit-tested in
isolation. Callers translate ``ScheduleValidationError`` into HTTP 400.

DST handling (PRD acceptance criteria): the next run is computed as a wall-clock
occurrence of (day, time) in the depot's IANA timezone, then converted to UTC.
A monthly 06:00 schedule therefore keeps firing at 06:00 *local* time across a
DST transition; the UTC instant shifts by the offset change. We trust the IANA
database via ``zoneinfo`` rather than hard-coding offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── Wire enums (camelCase values as the frontend Zod schemas expect) ──────────

VALID_KINDS = frozenset(
    {
        "weekly_ops",
        "monthly_savings",
        "monthly_consumption",
        "incident",
        "compliance",
        "ev_vs_diesel_tco",
    }
)
VALID_GROUP_BY = frozenset({"card", "vehicle"})
VALID_FREQUENCIES = frozenset({"weekly", "monthly", "quarterly"})
VALID_FORMATS = frozenset({"pdf", "csv"})
VALID_AUTONOMY_MODES = frozenset({"shadow", "proposed", "auto_notify", "auto_silent"})
VALID_RUN_STATUSES = frozenset({"succeeded", "failed", "skipped", "pending_approval"})
VALID_DELIVERY_STATUSES = frozenset({"sent", "failed", "bounced", "suppressed"})

# Calendar months a quarterly schedule fires in (calendar quarters).
_QUARTER_MONTHS = (1, 4, 7, 10)

# Resend webhook event status → frontend DeliveryStatus enum. The frontend only
# accepts {sent, failed, bounced, suppressed}; collapse the provider's richer
# vocabulary onto it (delivered is a success → sent; complaint → suppressed).
_PROVIDER_STATUS_TO_WIRE = {
    "sent": "sent",
    "delivered": "sent",
    "bounced": "bounced",
    "complained": "suppressed",
    "suppressed": "suppressed",
    "failed": "failed",
}


class ScheduleValidationError(ValueError):
    """Raised when a create/update payload is structurally invalid."""


def previous_month_bounds(now_local: datetime) -> tuple[str, str, str]:
    """Return (period_start, period_end, label) for the previous calendar month.

    Dates are YYYY-MM-DD in local time; label is 'Month YYYY'. Used by the
    report-schedule worker and reports.generate to default the period.
    """
    first_of_this_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return (
        first_of_prev.strftime("%Y-%m-%d"),
        last_of_prev.strftime("%Y-%m-%d"),
        first_of_prev.strftime("%B %Y"),
    )


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def compute_report_period(frequency: str, now_local: datetime) -> tuple[str, str]:
    """Return (period_start, period_end) date strings for the period a run covers.

    - monthly:   the previous full calendar month
    - weekly:    the seven days ending yesterday (local)
    - quarterly: the previous full calendar quarter (Jan-Mar / Apr-Jun / …)
    """
    if frequency == "monthly":
        start, end, _ = previous_month_bounds(now_local)
        return start, end
    if frequency == "weekly":
        end_date = (now_local - timedelta(days=1)).date()
        start_date = end_date - timedelta(days=6)
        return start_date.isoformat(), end_date.isoformat()
    if frequency == "quarterly":
        q_index = (now_local.month - 1) // 3  # 0..3 for current quarter
        if q_index == 0:
            prev_year, prev_q = now_local.year - 1, 3
        else:
            prev_year, prev_q = now_local.year, q_index - 1
        start_month = prev_q * 3 + 1
        end_month = start_month + 2
        start = datetime(prev_year, start_month, 1).date()
        end = datetime(prev_year, end_month, _last_day_of_month(prev_year, end_month)).date()
        return start.isoformat(), end.isoformat()
    raise ScheduleValidationError(f"Unknown frequency {frequency!r}")


def map_provider_status_to_wire(provider_status: str) -> str:
    """Map a Resend/webhook status onto the frontend DeliveryStatus enum.

    Unknown statuses fall back to ``failed`` so an unexpected provider event
    never produces an enum value the frontend would reject.
    """
    return _PROVIDER_STATUS_TO_WIRE.get(provider_status, "failed")


def parse_hh_mm(value: str) -> time:
    """Parse a ``"HH:mm"`` string into a ``time``. Raises ScheduleValidationError."""
    if not isinstance(value, str):
        raise ScheduleValidationError("timeOfDay must be a 'HH:mm' string")
    parts = value.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        raise ScheduleValidationError(f"timeOfDay {value!r} must be in 'HH:mm' format")
    if not (parts[0].isdigit() and parts[1].isdigit()):
        raise ScheduleValidationError(f"timeOfDay {value!r} must be in 'HH:mm' format")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleValidationError(f"timeOfDay {value!r} is out of range")
    return time(hour=hour, minute=minute)


def format_hh_mm(value: time) -> str:
    """Format a ``time`` as ``"HH:mm"``."""
    return f"{value.hour:02d}:{value.minute:02d}"


def resolve_timezone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA tz name, raising ScheduleValidationError on unknown zones."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ScheduleValidationError(f"Unknown timezone {tz_name!r}") from exc


def _localize_to_utc(year: int, month: int, day: int, t: time, tz: ZoneInfo) -> datetime:
    """Build a wall-clock local datetime and convert it to an aware UTC datetime."""
    local = datetime(year, month, day, t.hour, t.minute, 0, tzinfo=tz)
    return local.astimezone(timezone.utc)


def _spec_dow_to_python(day_of_week: int) -> int:
    """Convert spec weekday (0=Sun..6=Sat) to Python weekday (Mon=0..Sun=6)."""
    return (day_of_week - 1) % 7


def compute_next_run_at(
    now_utc: datetime,
    *,
    frequency: str,
    time_of_day: time,
    tz_name: str,
    day_of_month: int | None = None,
    day_of_week: int | None = None,
) -> datetime:
    """Return the next firing instant (aware UTC) strictly after ``now_utc``.

    ``now_utc`` must be timezone-aware. The cadence is interpreted in the depot
    timezone ``tz_name`` and converted to UTC, so DST shifts are handled by the
    IANA database.
    """
    if now_utc.tzinfo is None:
        raise ScheduleValidationError("now_utc must be timezone-aware")
    tz = resolve_timezone(tz_name)
    now_local = now_utc.astimezone(tz)

    if frequency == "weekly":
        if day_of_week is None:
            raise ScheduleValidationError("weekly schedules require dayOfWeek")
        return _next_weekly(now_local, day_of_week, time_of_day, tz)
    if frequency in ("monthly", "quarterly"):
        if day_of_month is None:
            raise ScheduleValidationError(f"{frequency} schedules require dayOfMonth")
        allowed_months = None if frequency == "monthly" else _QUARTER_MONTHS
        return _next_by_month(now_local, day_of_month, time_of_day, tz, allowed_months)
    raise ScheduleValidationError(f"Unknown frequency {frequency!r}")


def _next_weekly(now_local: datetime, day_of_week: int, t: time, tz: ZoneInfo) -> datetime:
    target_py = _spec_dow_to_python(day_of_week)
    days_ahead = (target_py - now_local.weekday()) % 7
    candidate_date = (now_local + timedelta(days=days_ahead)).date()
    candidate = _localize_to_utc(
        candidate_date.year, candidate_date.month, candidate_date.day, t, tz
    )
    if candidate <= now_local.astimezone(timezone.utc):
        candidate_date = candidate_date + timedelta(days=7)
        candidate = _localize_to_utc(
            candidate_date.year, candidate_date.month, candidate_date.day, t, tz
        )
    return candidate


def _next_by_month(
    now_local: datetime,
    day_of_month: int,
    t: time,
    tz: ZoneInfo,
    allowed_months: tuple[int, ...] | None,
) -> datetime:
    """First (allowed) month whose day_of_month@time is strictly after now.

    day_of_month is capped at 28 by the schema, so every month contains it and
    no clamping is needed. Scans forward month-by-month (bounded) to keep the
    quarterly logic trivial and explicit.
    """
    now_utc = now_local.astimezone(timezone.utc)
    year, month = now_local.year, now_local.month
    for _ in range(60):  # >4 years of monthly steps; ample headroom for quarterly
        if allowed_months is None or month in allowed_months:
            candidate = _localize_to_utc(year, month, day_of_month, t, tz)
            if candidate > now_utc:
                return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1
    # Unreachable in practice; defensive guard.
    raise ScheduleValidationError("Could not compute next run within horizon")


# ── Payload validation / normalization ────────────────────────────────────────


@dataclass(frozen=True)
class NormalizedRecipient:
    email_address: str
    format: str
    position: int


@dataclass(frozen=True)
class NormalizedScheduleInput:
    name: str
    kind: str
    group_by: str | None
    frequency: str
    day_of_month: int | None
    day_of_week: int | None
    time_of_day: time
    autonomy_mode: str
    is_active: bool
    recipients: list[NormalizedRecipient]


def _validate_email(value: object) -> str:
    if not isinstance(value, str) or "@" not in value:
        raise ScheduleValidationError(f"Invalid email address: {value!r}")
    local, _, domain = value.partition("@")
    if not local or "." not in domain or domain.endswith("."):
        raise ScheduleValidationError(f"Invalid email address: {value!r}")
    return value


def _validate_recipients(raw: object) -> list[NormalizedRecipient]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScheduleValidationError("recipients must be an array")
    out: list[NormalizedRecipient] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScheduleValidationError("each recipient must be an object")
        email = _validate_email(item.get("emailAddress") or item.get("email_address"))
        fmt = item.get("format")
        if fmt not in VALID_FORMATS:
            raise ScheduleValidationError(
                f"recipient format must be one of {sorted(VALID_FORMATS)}; got {fmt!r}"
            )
        key = (email.lower(), fmt)
        if key in seen:
            raise ScheduleValidationError(
                f"duplicate recipient ({email}, {fmt}) — (email, format) must be unique"
            )
        seen.add(key)
        out.append(NormalizedRecipient(email_address=email, format=fmt, position=position))
    return out


def _validate_cadence(
    frequency: str, day_of_month: object, day_of_week: object
) -> tuple[int | None, int | None]:
    """Enforce the weekly/monthly cadence column rules; returns (dom, dow)."""
    if frequency == "weekly":
        if day_of_week is None:
            raise ScheduleValidationError("weekly schedules require dayOfWeek")
        if (
            not isinstance(day_of_week, int)
            or isinstance(day_of_week, bool)
            or not (0 <= day_of_week <= 6)
        ):
            raise ScheduleValidationError("dayOfWeek must be an int in 0..6 (0=Sun)")
        if day_of_month is not None:
            raise ScheduleValidationError("weekly schedules must not set dayOfMonth")
        return None, day_of_week
    # monthly / quarterly
    if day_of_month is None:
        raise ScheduleValidationError(f"{frequency} schedules require dayOfMonth")
    if (
        not isinstance(day_of_month, int)
        or isinstance(day_of_month, bool)
        or not (1 <= day_of_month <= 28)
    ):
        raise ScheduleValidationError("dayOfMonth must be an int in 1..28")
    if day_of_week is not None:
        raise ScheduleValidationError(f"{frequency} schedules must not set dayOfWeek")
    return day_of_month, None


def _validate_kind_and_group_by(kind: object, group_by: object) -> tuple[str, str | None]:
    if kind not in VALID_KINDS:
        raise ScheduleValidationError(f"kind must be one of {sorted(VALID_KINDS)}; got {kind!r}")
    if group_by is not None and group_by not in VALID_GROUP_BY:
        raise ScheduleValidationError(
            f"groupBy must be one of {sorted(VALID_GROUP_BY)}; got {group_by!r}"
        )
    if kind == "monthly_consumption" and group_by is None:
        raise ScheduleValidationError("groupBy is required for kind 'monthly_consumption'")
    # The EV-vs-diesel comparison groups internally by vehicle type + fleet
    # total, so an explicit groupBy is meaningless here — reject it rather than
    # silently ignore so the wire contract stays unambiguous.
    if kind == "ev_vs_diesel_tco" and group_by is not None:
        raise ScheduleValidationError("groupBy is not supported for kind 'ev_vs_diesel_tco'")
    return kind, group_by  # type: ignore[return-value]


def normalize_create_payload(payload: object) -> NormalizedScheduleInput:
    """Validate + normalize a ScheduleCreatePayload (camelCase) into typed values."""
    if not isinstance(payload, dict):
        raise ScheduleValidationError("schedule input must be an object")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScheduleValidationError("name is required")

    frequency = payload.get("frequency")
    if frequency not in VALID_FREQUENCIES:
        raise ScheduleValidationError(
            f"frequency must be one of {sorted(VALID_FREQUENCIES)}; got {frequency!r}"
        )

    kind, group_by = _validate_kind_and_group_by(payload.get("kind"), payload.get("groupBy"))
    day_of_month, day_of_week = _validate_cadence(
        frequency, payload.get("dayOfMonth"), payload.get("dayOfWeek")
    )

    time_of_day_raw = payload.get("timeOfDay")
    if time_of_day_raw is None:
        raise ScheduleValidationError("timeOfDay is required")
    time_of_day = parse_hh_mm(time_of_day_raw)

    autonomy_mode = payload.get("autonomyMode")
    if autonomy_mode not in VALID_AUTONOMY_MODES:
        raise ScheduleValidationError(
            f"autonomyMode must be one of {sorted(VALID_AUTONOMY_MODES)}; got {autonomy_mode!r}"
        )

    is_active = payload.get("isActive", True)
    if not isinstance(is_active, bool):
        raise ScheduleValidationError("isActive must be a boolean")

    recipients = _validate_recipients(payload.get("recipients"))

    return NormalizedScheduleInput(
        name=name.strip(),
        kind=kind,
        group_by=group_by,
        frequency=frequency,
        day_of_month=day_of_month,
        day_of_week=day_of_week,
        time_of_day=time_of_day,
        autonomy_mode=autonomy_mode,
        is_active=is_active,
        recipients=recipients,
    )


def normalize_patch_payload(patch: object, *, current: dict) -> NormalizedScheduleInput:
    """Validate a SchedulePatchPayload by overlaying it on the current row.

    ``current`` is the existing schedule as python values (keys: name, kind,
    group_by, frequency, day_of_month, day_of_week, time_of_day(time),
    autonomy_mode, is_active). The returned ``recipients`` reflect the patch's
    recipients (validated) when present, else an empty list. The caller decides
    whether to replace recipients by testing ``"recipients" in patch`` itself —
    an absent key means "leave recipients untouched", not "clear them".
    """
    if not isinstance(patch, dict):
        raise ScheduleValidationError("patch must be an object")

    merged: dict = {
        "name": current["name"],
        "kind": current["kind"],
        "groupBy": current["group_by"],
        "frequency": current["frequency"],
        "dayOfMonth": current["day_of_month"],
        "dayOfWeek": current["day_of_week"],
        "timeOfDay": format_hh_mm(current["time_of_day"]),
        "autonomyMode": current["autonomy_mode"],
        "isActive": current["is_active"],
    }
    # Overlay only keys actually present in the patch.
    for wire_key in (
        "name",
        "kind",
        "groupBy",
        "frequency",
        "dayOfMonth",
        "dayOfWeek",
        "timeOfDay",
        "autonomyMode",
        "isActive",
    ):
        if wire_key in patch:
            merged[wire_key] = patch[wire_key]

    # When frequency changes, the caller may not have nulled the now-irrelevant
    # day field. Clear the mismatched one so cadence validation is coherent.
    if patch.get("frequency") == "weekly" and "dayOfMonth" not in patch:
        merged["dayOfMonth"] = None
    if patch.get("frequency") in ("monthly", "quarterly") and "dayOfWeek" not in patch:
        merged["dayOfWeek"] = None

    merged["recipients"] = patch.get("recipients", [])
    return normalize_create_payload(merged)
