"""Scheduled-reports runtime: repo, serializers, run execution, cron worker.

Deliberately decoupled from ``src/api/main.py``:
- report generation is injected as a ``generate_report`` callable, so this
  module never imports the FastAPI app (no circular import);
- ``asyncpg`` is only needed for type hints (guarded under ``TYPE_CHECKING``),
  so the pure repo/serializer logic can be unit-tested with mock connections.

Wire serialization uses plain dicts (camelCase keys) rather than Pydantic so the
strict-null contract the frontend Zod schemas require (nextRunAt / lastRunAt /
lastRunStatus / recipients[].lastDelivery always present, recipients always an
array) is explicit and directly testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .report_schedule_timing import (
    NormalizedScheduleInput,
    compute_next_run_at,
    compute_report_period,
    format_hh_mm,
    map_provider_status_to_wire,
    previous_month_bounds,
    resolve_timezone,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

    from ..db.pools import DatabasePools
    from ..notifications.email_client import EmailDeliveryClient

logger = logging.getLogger(__name__)

# (params, depot_id) -> report_id
GenerateReportFn = Callable[[dict, str], Awaitable[str]]
# depot_id -> IANA timezone name
GetTimezoneFn = Callable[[str], Awaitable[str]]

REPORT_DRAFT_ACTION_CLASS = "report_draft"
_WORKER_INTERVAL_S = 60


# ── ISO-8601 UTC formatting ───────────────────────────────────────────────────


def _iso_z(value: Optional[datetime]) -> Optional[str]:
    """Format an aware datetime as an ISO-8601 UTC string ending in 'Z'."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Serializers (row -> wire dict) ────────────────────────────────────────────


def delivery_to_wire(row: "asyncpg.Record") -> dict:
    # recipient_id is null when the recipient was later removed (FK ON DELETE
    # SET NULL keeps the historical delivery row); the snapshotted email_address
    # / status still describe the attempt.
    return {
        "recipientId": str(row["recipient_id"]) if row["recipient_id"] is not None else None,
        "emailAddress": row["email_address"],
        "format": row["format"],
        "status": row["status"],
        "attemptedAt": _iso_z(row["attempted_at"]),
        "providerMessageId": row["provider_message_id"],
        "error": row["error"],
    }


def recipient_to_wire(row: "asyncpg.Record", last_delivery: Optional[dict]) -> dict:
    return {
        "recipientId": str(row["id"]),
        "emailAddress": row["email_address"],
        "format": row["format"],
        "lastDelivery": last_delivery,
    }


def schedule_to_wire(row: "asyncpg.Record", recipients: list[dict]) -> dict:
    return {
        "id": str(row["id"]),
        "depotId": str(row["depot_id"]),
        "name": row["name"],
        "kind": row["kind"],
        "groupBy": row["group_by"],
        "frequency": row["frequency"],
        "dayOfMonth": row["day_of_month"],
        "dayOfWeek": row["day_of_week"],
        "timeOfDay": format_hh_mm(row["time_of_day"]),
        "autonomyMode": row["autonomy_mode"],
        "isActive": row["is_active"],
        "recipients": recipients,
        "nextRunAt": _iso_z(row["next_run_at"]),
        "lastRunAt": _iso_z(row["last_run_at"]),
        "lastRunStatus": row["last_run_status"],
        "createdAt": _iso_z(row["created_at"]),
        "createdBy": str(row["created_by"]) if row["created_by"] is not None else None,
        "updatedAt": _iso_z(row["updated_at"]),
    }


def run_to_wire(row: "asyncpg.Record", deliveries: list[dict]) -> dict:
    return {
        "runId": str(row["id"]),
        "scheduleId": str(row["schedule_id"]),
        "reportId": str(row["report_id"]) if row["report_id"] is not None else None,
        "triggeredAt": _iso_z(row["triggered_at"]),
        "completedAt": _iso_z(row["completed_at"]),
        "status": row["status"],
        "deliveries": deliveries,
        "errorMessage": row["error_message"],
    }


# ── Autonomy resolution ───────────────────────────────────────────────────────


async def resolve_autonomy_mode(
    conn: "asyncpg.Connection", depot_id: str, action_class: str, fallback: str
) -> str:
    """Per-(depot, action_class) autonomy override, falling back to ``fallback``.

    Reads the shared ``agent_autonomy_settings`` matrix (PR #233): one row per
    (depot, action_class) with a ``level`` in the same four-value vocabulary as
    a schedule's autonomy_mode. If that table is not present yet (different merge
    order), fall back to the per-schedule mode rather than erroring — undefined
    table is SQLSTATE 42P01. Called on a bare connection (no open transaction),
    so a swallowed error does not poison a transaction.
    """
    try:
        row = await conn.fetchrow(
            "SELECT level FROM agent_autonomy_settings "
            "WHERE depot_id = $1::uuid AND action_class = $2",
            depot_id,
            action_class,
        )
    except Exception as exc:  # noqa: BLE001 - tolerate the table not existing yet
        if getattr(exc, "sqlstate", None) == "42P01":
            return fallback
        raise
    return row["level"] if row else fallback


# ── Repo: schedules + recipients ──────────────────────────────────────────────

_SCHEDULE_COLUMNS = (
    "id, depot_id, name, kind, group_by, frequency, day_of_month, day_of_week, "
    "time_of_day, autonomy_mode, is_active, next_run_at, last_run_at, "
    "last_run_status, created_at, created_by, updated_at"
)


async def insert_schedule(
    conn: "asyncpg.Connection",
    *,
    depot_id: str,
    norm: NormalizedScheduleInput,
    next_run_at: Optional[datetime],
    created_by: Optional[str],
) -> "asyncpg.Record":
    return await conn.fetchrow(
        f"""
        INSERT INTO report_schedules
            (depot_id, name, kind, group_by, frequency, day_of_month, day_of_week,
             time_of_day, autonomy_mode, is_active, next_run_at, created_by)
        VALUES
            ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::uuid)
        RETURNING {_SCHEDULE_COLUMNS}
        """,
        depot_id,
        norm.name,
        norm.kind,
        norm.group_by,
        norm.frequency,
        norm.day_of_month,
        norm.day_of_week,
        norm.time_of_day,
        norm.autonomy_mode,
        norm.is_active,
        next_run_at,
        created_by,
    )


async def update_schedule_fields(
    conn: "asyncpg.Connection",
    *,
    schedule_id: str,
    norm: NormalizedScheduleInput,
    next_run_at: Optional[datetime],
) -> "asyncpg.Record":
    return await conn.fetchrow(
        f"""
        UPDATE report_schedules
        SET name = $2, kind = $3, group_by = $4, frequency = $5,
            day_of_month = $6, day_of_week = $7, time_of_day = $8,
            autonomy_mode = $9, is_active = $10, next_run_at = $11,
            updated_at = NOW()
        WHERE id = $1::uuid
        RETURNING {_SCHEDULE_COLUMNS}
        """,
        schedule_id,
        norm.name,
        norm.kind,
        norm.group_by,
        norm.frequency,
        norm.day_of_month,
        norm.day_of_week,
        norm.time_of_day,
        norm.autonomy_mode,
        norm.is_active,
        next_run_at,
    )


async def replace_recipients(
    conn: "asyncpg.Connection", schedule_id: str, recipients: list
) -> None:
    """Reconcile the recipient list: upsert present rows (preserving ids and
    therefore delivery history), delete those no longer present.

    ``recipients`` is a list of NormalizedRecipient. Stable recipient ids are
    important so post-send webhook callbacks can still attribute deliveries
    after an edit, and so unchanged recipients keep their lastDelivery.
    """
    existing = await conn.fetch(
        "SELECT id, email_address, format FROM report_schedule_recipients WHERE schedule_id = $1::uuid",
        schedule_id,
    )
    existing_keys = {(r["email_address"], r["format"]) for r in existing}
    incoming_keys = {(r.email_address, r.format) for r in recipients}

    # Delete recipients removed from the list (cascades their deliveries).
    to_delete = existing_keys - incoming_keys
    for email, fmt in to_delete:
        await conn.execute(
            "DELETE FROM report_schedule_recipients "
            "WHERE schedule_id = $1::uuid AND email_address = $2 AND format = $3",
            schedule_id,
            email,
            fmt,
        )

    # Upsert each incoming recipient at its new position.
    for rec in recipients:
        await conn.execute(
            """
            INSERT INTO report_schedule_recipients (schedule_id, email_address, format, position)
            VALUES ($1::uuid, $2, $3, $4)
            ON CONFLICT (schedule_id, email_address, format)
            DO UPDATE SET position = EXCLUDED.position
            """,
            schedule_id,
            rec.email_address,
            rec.format,
            rec.position,
        )


async def fetch_schedule_row(
    conn: "asyncpg.Connection", depot_id: str, schedule_id: str
) -> Optional["asyncpg.Record"]:
    return await conn.fetchrow(
        f"SELECT {_SCHEDULE_COLUMNS} FROM report_schedules "
        f"WHERE id = $1::uuid AND depot_id = $2::uuid",
        schedule_id,
        depot_id,
    )


async def delete_schedule(conn: "asyncpg.Connection", depot_id: str, schedule_id: str) -> bool:
    row = await conn.fetchrow(
        "DELETE FROM report_schedules WHERE id = $1::uuid AND depot_id = $2::uuid RETURNING id",
        schedule_id,
        depot_id,
    )
    return row is not None


async def _recipients_by_schedule(
    conn: "asyncpg.Connection", schedule_ids: list[str]
) -> dict[str, list["asyncpg.Record"]]:
    if not schedule_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, schedule_id, email_address, format, position "
        "FROM report_schedule_recipients WHERE schedule_id = ANY($1::uuid[]) "
        "ORDER BY schedule_id, position",
        schedule_ids,
    )
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(str(r["schedule_id"]), []).append(r)
    return out


async def _last_deliveries_by_schedule(
    conn: "asyncpg.Connection", schedule_ids: list[str]
) -> dict[tuple[str, str, str], dict]:
    """Latest delivery per (schedule_id, email_address, format) → wire lastDelivery."""
    if not schedule_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (r.schedule_id, d.email_address, d.format)
               r.schedule_id, d.email_address, d.format, d.status, d.attempted_at
        FROM schedule_run_deliveries d
        JOIN schedule_runs r ON r.id = d.run_id
        WHERE r.schedule_id = ANY($1::uuid[])
        ORDER BY r.schedule_id, d.email_address, d.format, d.attempted_at DESC
        """,
        schedule_ids,
    )
    out: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (str(r["schedule_id"]), r["email_address"], r["format"])
        out[key] = {"status": r["status"], "attemptedAt": _iso_z(r["attempted_at"])}
    return out


def _assemble_schedule_wire(
    schedule_row: "asyncpg.Record",
    recipient_rows: list["asyncpg.Record"],
    last_deliveries: dict[tuple[str, str, str], dict],
) -> dict:
    schedule_id = str(schedule_row["id"])
    recipients = [
        recipient_to_wire(
            rec,
            last_deliveries.get((schedule_id, rec["email_address"], rec["format"])),
        )
        for rec in recipient_rows
    ]
    return schedule_to_wire(schedule_row, recipients)


async def serialize_schedule(
    conn: "asyncpg.Connection", depot_id: str, schedule_id: str
) -> Optional[dict]:
    row = await fetch_schedule_row(conn, depot_id, schedule_id)
    if row is None:
        return None
    recipients = (await _recipients_by_schedule(conn, [schedule_id])).get(schedule_id, [])
    last = await _last_deliveries_by_schedule(conn, [schedule_id])
    return _assemble_schedule_wire(row, recipients, last)


async def serialize_schedules(conn: "asyncpg.Connection", depot_id: str) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT {_SCHEDULE_COLUMNS} FROM report_schedules "
        f"WHERE depot_id = $1::uuid ORDER BY created_at DESC",
        depot_id,
    )
    schedule_ids = [str(r["id"]) for r in rows]
    recipients_map = await _recipients_by_schedule(conn, schedule_ids)
    last = await _last_deliveries_by_schedule(conn, schedule_ids)
    return [
        _assemble_schedule_wire(row, recipients_map.get(str(row["id"]), []), last) for row in rows
    ]


# ── Repo: runs + deliveries ───────────────────────────────────────────────────

_RUN_COLUMNS = (
    "id, schedule_id, report_id, scheduled_for, triggered_at, completed_at, "
    "status, error_message"
)


async def claim_run(
    conn: "asyncpg.Connection", schedule_id: str, scheduled_for: datetime
) -> Optional[str]:
    """Insert a run row for (schedule, slot); return its id, or None if the slot
    was already claimed (the UNIQUE constraint is the cron idempotency anchor).

    The initial status is 'skipped' with no ``completed_at`` — a placeholder until
    ``finalize_run`` writes the real outcome. Conflicting claims are ignored (no
    stale reclaim) so an in-flight or partially-finished run cannot be executed
    twice for the same slot.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO schedule_runs (schedule_id, scheduled_for, status)
        VALUES ($1::uuid, $2, 'skipped')
        ON CONFLICT (schedule_id, scheduled_for) DO NOTHING
        RETURNING id::text
        """,
        schedule_id,
        scheduled_for,
    )
    return row["id"] if row else None


async def _update_schedule_last_run_status(
    conn: "asyncpg.Connection", run_id: str, status: str
) -> None:
    """Keep ``report_schedules.last_run_status`` in sync after a run is resolved.

    Only mutates the summary when this run is the schedule's most recent slot, so
    resolving an older pending draft (approved/rejected out of order) cannot
    regress ``last_run_status`` behind a newer run that already ran.
    """
    await conn.execute(
        """
        UPDATE report_schedules rs
        SET last_run_status = $2, last_run_at = sr.scheduled_for, updated_at = NOW()
        FROM schedule_runs sr
        WHERE sr.id = $1::uuid
          AND rs.id = sr.schedule_id
          AND sr.scheduled_for >= (
              SELECT MAX(scheduled_for) FROM schedule_runs WHERE schedule_id = sr.schedule_id
          )
        """,
        run_id,
        status,
    )


async def finalize_run(
    conn: "asyncpg.Connection",
    run_id: str,
    *,
    status: str,
    report_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        UPDATE schedule_runs
        SET status = $2, report_id = $3::uuid, error_message = $4, completed_at = NOW()
        WHERE id = $1::uuid
        """,
        run_id,
        status,
        report_id,
        error_message,
    )


async def insert_delivery(
    conn: "asyncpg.Connection",
    *,
    run_id: str,
    recipient_id: Optional[str],
    email_address: str,
    fmt: str,
    status: str,
    provider_message_id: Optional[str],
    error: Optional[str],
) -> None:
    await conn.execute(
        """
        INSERT INTO schedule_run_deliveries
            (run_id, recipient_id, email_address, format, status, provider_message_id, error)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
        """,
        run_id,
        recipient_id,
        email_address,
        fmt,
        status,
        provider_message_id,
        error,
    )


async def fetch_run(conn: "asyncpg.Connection", run_id: str) -> Optional["asyncpg.Record"]:
    return await conn.fetchrow(
        f"SELECT {_RUN_COLUMNS} FROM schedule_runs WHERE id = $1::uuid", run_id
    )


_RUN_FINALIZE_POLL_INTERVAL_S = 0.25
_RUN_FINALIZE_WAIT_TIMEOUT_S = 300.0


async def wait_for_run_finalized(
    pools: "DatabasePools",
    run_id: str,
    *,
    poll_interval_s: float = _RUN_FINALIZE_POLL_INTERVAL_S,
    timeout_s: float = _RUN_FINALIZE_WAIT_TIMEOUT_S,
) -> None:
    """Block until ``finalize_run`` sets ``completed_at`` (placeholder rows omit it)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        async with pools.ts.acquire() as conn:
            completed_at = await conn.fetchval(
                "SELECT completed_at FROM schedule_runs WHERE id = $1::uuid",
                run_id,
            )
        if completed_at is not None:
            return
        await asyncio.sleep(poll_interval_s)
    logger.warning(
        "wait_for_run_finalized timed out for run_id=%s after %.0fs",
        run_id,
        timeout_s,
    )


async def serialize_run(conn: "asyncpg.Connection", run_id: str) -> Optional[dict]:
    run = await fetch_run(conn, run_id)
    if run is None:
        return None
    deliveries = await conn.fetch(
        "SELECT recipient_id, email_address, format, status, attempted_at, "
        "provider_message_id, error FROM schedule_run_deliveries "
        "WHERE run_id = $1::uuid ORDER BY attempted_at, email_address",
        run_id,
    )
    return run_to_wire(run, [delivery_to_wire(d) for d in deliveries])


async def serialize_runs(conn: "asyncpg.Connection", schedule_id: str) -> list[dict]:
    runs = await conn.fetch(
        f"SELECT {_RUN_COLUMNS} FROM schedule_runs "
        f"WHERE schedule_id = $1::uuid ORDER BY triggered_at DESC",
        schedule_id,
    )
    run_ids = [str(r["id"]) for r in runs]
    deliveries_map: dict[str, list] = {}
    if run_ids:
        del_rows = await conn.fetch(
            "SELECT run_id, recipient_id, email_address, format, status, attempted_at, "
            "provider_message_id, error FROM schedule_run_deliveries "
            "WHERE run_id = ANY($1::uuid[]) ORDER BY attempted_at, email_address",
            run_ids,
        )
        for d in del_rows:
            deliveries_map.setdefault(str(d["run_id"]), []).append(delivery_to_wire(d))
    return [run_to_wire(run, deliveries_map.get(str(run["id"]), [])) for run in runs]


async def emit_report_draft_action(
    conn: "asyncpg.Connection",
    *,
    depot_id: str,
    schedule_id: str,
    report_id: Optional[str],
    run_id: str,
    mode: str,
    status: str,
    period_start: str,
    period_end: str,
    kind: str,
    group_by: Optional[str],
    title: str,
    summary: str,
) -> bool:
    """Emit the schedule-originated report_draft agent_action.

    payload carries scheduleId + runId so agents.action.approve/reject can find
    and resolve the originating run. The (depot_id, scheduleId, periodStart)
    uniqueness (migration 044) prevents duplicate drafts for one slot.

    Returns True when a new row was inserted, False when a draft for the same
    (depot, schedule, period) already exists.
    """
    payload = {
        "kind": kind,
        "groupBy": group_by,
        "periodStart": period_start,
        "periodEnd": period_end,
        "title": title,
        "scheduleId": schedule_id,
        "runId": run_id,
        "reportId": report_id,
    }
    row = await conn.fetchrow(
        """
        INSERT INTO agent_actions
            (depot_id, agent_type, action_class, mode, status, summary, payload)
        VALUES ($1::uuid, 'reporting', 'report_draft', $2, $3, $4, $5::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id::text
        """,
        depot_id,
        mode,
        status,
        summary,
        json.dumps(payload),
    )
    return row is not None


# ── Delivery ──────────────────────────────────────────────────────────────────


def _parse_report_data(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _render_attachment(fmt: str, report_row: "asyncpg.Record", parsed_data: Optional[dict]):
    """Render the report into an EmailAttachment for the requested format."""
    from ..notifications.email_client import EmailAttachment  # lazy: keep import local

    kind = report_row["kind"]
    period = ""
    if report_row["period_start"] is not None:
        period = report_row["period_start"].date().isoformat()
    base_name = f"{kind}_{period}".strip("_") or "report"

    if fmt == "pdf":
        from .report_pdf import render_report_pdf  # lazy: only PDF delivery needs reportlab

        depot_name = (parsed_data or {}).get("depot_name") or str(report_row["depot_id"])
        content = render_report_pdf(
            title=report_row["title"],
            depot_name=depot_name,
            kind=kind,
            period_start=report_row["period_start"],
            period_end=report_row["period_end"],
            group_by=report_row["group_by"],
            data=parsed_data,
        )
        return EmailAttachment(
            filename=f"{base_name}.pdf", content=content, content_type="application/pdf"
        )

    # CSV: reuse the same streamer the export endpoint uses.
    from .reports import stream_rows_as_csv

    rows = (parsed_data or {}).get("rows", [])
    totals = (parsed_data or {}).get("totals")
    csv_text = "".join(stream_rows_as_csv(rows, group_by=report_row["group_by"], totals=totals))
    return EmailAttachment(
        filename=f"{base_name}.csv", content=csv_text.encode("utf-8"), content_type="text/csv"
    )


async def _deliver_to_recipients(
    pools: "DatabasePools",
    *,
    run_id: str,
    report_row: "asyncpg.Record",
    recipient_rows: list["asyncpg.Record"],
    email_client: "EmailDeliveryClient",
    default_from: str,
) -> bool:
    """Render + send to each recipient and record one delivery row per attempt.

    Email I/O happens outside any DB transaction; each delivery row is written
    in its own short statement so a provider hiccup mid-list still records the
    attempts that did complete.

    Returns True when every recipient was sent successfully (or there are no
    recipients to deliver to).
    """
    from ..notifications.email_client import EmailMessage  # lazy

    parsed_data = _parse_report_data(report_row["data"])
    title = report_row["title"]
    period_label = ""
    if report_row["period_start"] is not None and report_row["period_end"] is not None:
        period_label = (
            f"{report_row['period_start'].date().isoformat()} – "
            f"{report_row['period_end'].date().isoformat()}"
        )
    body_text = f"Your scheduled report '{title}' is attached.\n\nPeriod: {period_label}\n"
    body_html = (
        f"<p>Your scheduled report <strong>{title}</strong> is attached.</p>"
        f"<p>Period: {period_label}</p>"
    )

    # Recipients already delivered for this run (e.g. a prior approve attempt that
    # partially succeeded) must not be emailed again on retry.
    async with pools.ts.acquire() as conn:
        sent_rows = await conn.fetch(
            "SELECT DISTINCT recipient_id FROM schedule_run_deliveries "
            "WHERE run_id = $1::uuid AND status = 'sent' AND recipient_id IS NOT NULL",
            run_id,
        )
    already_sent = {str(r["recipient_id"]) for r in sent_rows}

    all_sent = True
    for rec in recipient_rows:
        if str(rec["id"]) in already_sent:
            continue  # already delivered on a previous attempt
        fmt = rec["format"]
        status = "failed"
        provider_message_id: Optional[str] = None
        error: Optional[str] = None
        try:
            attachment = _render_attachment(fmt, report_row, parsed_data)
            message = EmailMessage(
                to=rec["email_address"],
                subject=title,
                html=body_html,
                text=body_text,
                from_address=default_from,
                attachments=[attachment],
            )
            result = await email_client.send(message)
            if result.ok:
                status = "sent"
                provider_message_id = result.provider_message_id
            else:
                error = json.dumps(result.detail) if result.detail else "send_failed"
        except Exception as exc:  # noqa: BLE001 - record any render/send error per recipient
            error = str(exc)
            logger.warning(
                "report delivery failed for run=%s recipient=%s: %s", run_id, rec["id"], exc
            )

        if status != "sent":
            all_sent = False
        async with pools.ts.acquire() as conn:
            await insert_delivery(
                conn,
                run_id=run_id,
                recipient_id=str(rec["id"]),
                email_address=rec["email_address"],
                fmt=fmt,
                status=status,
                provider_message_id=provider_message_id,
                error=error,
            )
    return all_sent


async def _fetch_report_row(pools: "DatabasePools", report_id: str) -> Optional["asyncpg.Record"]:
    async with pools.ts.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, depot_id, title, kind, group_by, period_start, period_end, data "
            "FROM reports WHERE id = $1::uuid",
            report_id,
        )


async def _fetch_recipient_rows(pools: "DatabasePools", schedule_id: str) -> list["asyncpg.Record"]:
    async with pools.ts.acquire() as conn:
        return await conn.fetch(
            "SELECT id, email_address, format FROM report_schedule_recipients "
            "WHERE schedule_id = $1::uuid ORDER BY position",
            schedule_id,
        )


# ── Run execution ─────────────────────────────────────────────────────────────


async def execute_schedule_run(
    pools: "DatabasePools",
    *,
    schedule_row: "asyncpg.Record",
    run_id: str,
    tz_name: str,
    email_client: "EmailDeliveryClient",
    generate_report: GenerateReportFn,
    default_from: str,
    now_utc: datetime,
    scheduled_for: Optional[datetime] = None,
) -> str:
    """Execute a claimed run end-to-end and return its terminal status.

    Honours the resolved autonomy mode:
      shadow        → skipped (no report, no delivery)
      proposed      → generate report, emit pending report_draft, pending_approval
      auto_notify   → generate report, deliver, emit executed report_draft, succeeded
      auto_silent   → generate report, deliver, succeeded (no agent action)
    """
    depot_id = str(schedule_row["depot_id"])
    schedule_id = str(schedule_row["id"])
    tz = resolve_timezone(tz_name)
    period_anchor = (scheduled_for if scheduled_for is not None else now_utc).astimezone(tz)

    async with pools.ts.acquire() as conn:
        mode = await resolve_autonomy_mode(
            conn, depot_id, REPORT_DRAFT_ACTION_CLASS, schedule_row["autonomy_mode"]
        )

    if mode == "shadow":
        async with pools.ts.acquire() as conn:
            await finalize_run(conn, run_id, status="skipped")
        return "skipped"

    # Generate the report (any kind). On failure mark the run failed.
    period_start, period_end = compute_report_period(schedule_row["frequency"], period_anchor)
    title = f"{schedule_row['name']} — {period_start} to {period_end}"
    params = {
        "kind": schedule_row["kind"],
        "groupBy": schedule_row["group_by"],
        "title": title,
        "periodStart": period_start,
        "periodEnd": period_end,
    }
    try:
        report_id = await generate_report(params, depot_id)
    except Exception as exc:  # noqa: BLE001 - report generation failure is a run failure
        logger.error(
            "report generation failed for schedule=%s: %s", schedule_id, exc, exc_info=True
        )
        async with pools.ts.acquire() as conn:
            await finalize_run(conn, run_id, status="failed", error_message=str(exc))
        return "failed"

    if mode == "proposed":
        async with pools.ts.acquire() as conn:
            async with conn.transaction():
                draft_inserted = await emit_report_draft_action(
                    conn,
                    depot_id=depot_id,
                    schedule_id=schedule_id,
                    report_id=report_id,
                    run_id=run_id,
                    mode="proposed",
                    status="pending",
                    period_start=period_start,
                    period_end=period_end,
                    kind=schedule_row["kind"],
                    group_by=schedule_row["group_by"],
                    title=title,
                    summary=f"Approve to send '{schedule_row['name']}' for {period_start}–{period_end}",
                )
                if draft_inserted:
                    await finalize_run(conn, run_id, status="pending_approval", report_id=report_id)
                else:
                    await finalize_run(
                        conn,
                        run_id,
                        status="skipped",
                        report_id=report_id,
                        error_message=(
                            "A report draft for this schedule and period is already "
                            "pending approval"
                        ),
                    )
        return "pending_approval" if draft_inserted else "skipped"

    # auto_notify / auto_silent → deliver now.
    report_row = await _fetch_report_row(pools, report_id)
    recipient_rows = await _fetch_recipient_rows(pools, schedule_id)
    if report_row is not None and recipient_rows:
        await _deliver_to_recipients(
            pools,
            run_id=run_id,
            report_row=report_row,
            recipient_rows=recipient_rows,
            email_client=email_client,
            default_from=default_from,
        )

    if mode == "auto_notify":
        # The report is already delivered; the informational action is a
        # best-effort feed entry. A failure here must not flip a delivered run
        # to 'failed' (which would trigger retries + duplicate sends).
        try:
            async with pools.ts.acquire() as conn:
                await emit_report_draft_action(
                    conn,
                    depot_id=depot_id,
                    schedule_id=schedule_id,
                    report_id=report_id,
                    run_id=run_id,
                    mode="auto_notify",
                    status="executed",
                    period_start=period_start,
                    period_end=period_end,
                    kind=schedule_row["kind"],
                    group_by=schedule_row["group_by"],
                    title=title,
                    summary=f"Sent '{schedule_row['name']}' for {period_start}–{period_end}",
                )
        except Exception as exc:  # noqa: BLE001 - informational write is non-blocking
            logger.warning(
                "report worker: auto_notify action write failed (delivery already done) "
                "schedule=%s: %s",
                schedule_id,
                exc,
            )

    async with pools.ts.acquire() as conn:
        await finalize_run(conn, run_id, status="succeeded", report_id=report_id)
    return "succeeded"


async def deliver_pending_run(
    pools: "DatabasePools",
    *,
    run_id: str,
    email_client: "EmailDeliveryClient",
    default_from: str,
) -> tuple[Optional[dict], bool]:
    """Deliver a run that was held in 'pending_approval' (proposed mode → approved).

    Idempotent: only acts on a run still in 'pending_approval'. Returns the run's
    wire dict (or None if the run no longer exists) and whether delivery succeeded
    (all recipients sent, or nothing to deliver).
    """
    async with pools.ts.acquire() as conn:
        run = await fetch_run(conn, run_id)
    if run is None:
        return None, False
    if run["status"] != "pending_approval" or run["report_id"] is None:
        # Already resolved (or never had a report) — return current state.
        async with pools.ts.acquire() as conn:
            wire = await serialize_run(conn, run_id)
        return wire, wire is not None and wire.get("status") == "succeeded"

    report_row = await _fetch_report_row(pools, str(run["report_id"]))
    recipient_rows = await _fetch_recipient_rows(pools, str(run["schedule_id"]))
    if not recipient_rows:
        delivery_ok = True
    elif report_row is None:
        delivery_ok = False
    else:
        delivery_ok = await _deliver_to_recipients(
            pools,
            run_id=run_id,
            report_row=report_row,
            recipient_rows=recipient_rows,
            email_client=email_client,
            default_from=default_from,
        )

    async with pools.ts.acquire() as conn:
        if delivery_ok:
            updated = await conn.fetchrow(
                "UPDATE schedule_runs SET status = 'succeeded', completed_at = NOW() "
                "WHERE id = $1::uuid AND status = 'pending_approval' "
                "RETURNING schedule_id::text",
                run_id,
            )
            if updated is not None:
                await _update_schedule_last_run_status(conn, run_id, "succeeded")
        return await serialize_run(conn, run_id), delivery_ok


async def skip_pending_run(pools: "DatabasePools", *, run_id: str) -> None:
    """Flip a pending_approval run to skipped (proposed mode → rejected)."""
    async with pools.ts.acquire() as conn:
        updated = await conn.fetchrow(
            "UPDATE schedule_runs SET status = 'skipped', completed_at = NOW() "
            "WHERE id = $1::uuid AND status = 'pending_approval' "
            "RETURNING schedule_id::text",
            run_id,
        )
        if updated is not None:
            await _update_schedule_last_run_status(conn, run_id, "skipped")


# ── Webhook: append delivery status ───────────────────────────────────────────


async def append_delivery_status_from_webhook(
    conn: "asyncpg.Connection",
    *,
    provider_message_id: str,
    provider_status: str,
    detail: Optional[dict],
) -> bool:
    """On a provider callback, append a NEW delivery row mirroring the original
    send but with the updated (mapped) status, so lastDelivery reflects it.

    Resend events for one send can arrive out of order. A terminal-negative
    state (bounced / suppressed / failed) is final for that send, so a late
    positive event ('sent'/'delivered' → 'sent') is dropped rather than appended
    — otherwise it would mask the bounce in lastDelivery. A new run sends a new
    provider_message_id, so genuine re-delivery after a bounce is unaffected.

    Returns True if a matching report delivery was found.
    """
    latest = await conn.fetchrow(
        """
        SELECT run_id, recipient_id, email_address, format, status
        FROM schedule_run_deliveries
        WHERE provider_message_id = $1
        ORDER BY attempted_at DESC
        LIMIT 1
        """,
        provider_message_id,
    )
    if latest is None:
        return False

    mapped = map_provider_status_to_wire(provider_status)
    if latest["status"] in ("bounced", "suppressed", "failed") and mapped == "sent":
        # Out-of-order positive event after a terminal-negative one — ignore.
        return True

    await insert_delivery(
        conn,
        run_id=str(latest["run_id"]),
        recipient_id=(
            str(latest["recipient_id"]) if latest["recipient_id"] is not None else None
        ),
        email_address=latest["email_address"],
        fmt=latest["format"],
        status=mapped,
        provider_message_id=provider_message_id,
        error=json.dumps(detail) if detail else None,
    )
    return True


# ── Worker ─────────────────────────────────────────────────────────────────────


async def _tick(
    pools: "DatabasePools",
    *,
    email_client: "EmailDeliveryClient",
    generate_report: GenerateReportFn,
    get_timezone: GetTimezoneFn,
    default_from: str,
    now_utc: Optional[datetime] = None,
) -> int:
    """Process all due schedules once. Returns the number of runs executed."""
    now_utc = now_utc or datetime.now(timezone.utc)
    async with pools.ts.acquire() as conn:
        due = await conn.fetch(
            f"SELECT {_SCHEDULE_COLUMNS} FROM report_schedules "
            f"WHERE is_active AND next_run_at IS NOT NULL AND next_run_at <= $1",
            now_utc,
        )

    executed = 0
    for schedule in due:
        schedule_id = str(schedule["id"])
        scheduled_for = schedule["next_run_at"]
        try:
            tz_name = await get_timezone(str(schedule["depot_id"]))
        except Exception as exc:  # noqa: BLE001 - transient/static failure
            # Timezone unresolved (depot deleted or static DB unreachable). Leave
            # next_run_at untouched so the slot stays due and a transient outage is
            # retried on the next tick rather than silently dropped.
            logger.warning(
                "report worker: cannot resolve tz for schedule %s; leaving slot due: %s",
                schedule_id,
                exc,
            )
            continue

        async with pools.ts.acquire() as conn:
            run_id = await claim_run(conn, schedule_id, scheduled_for)

        terminal_status: Optional[str] = None
        if run_id is not None:
            try:
                terminal_status = await execute_schedule_run(
                    pools,
                    schedule_row=schedule,
                    run_id=run_id,
                    tz_name=tz_name,
                    email_client=email_client,
                    generate_report=generate_report,
                    default_from=default_from,
                    now_utc=now_utc,
                    scheduled_for=scheduled_for,
                )
                executed += 1
            except Exception as exc:  # noqa: BLE001 - one bad run must not stall the worker
                logger.error(
                    "report worker: run failed schedule=%s: %s", schedule_id, exc, exc_info=True
                )
                async with pools.ts.acquire() as conn:
                    await finalize_run(conn, run_id, status="failed", error_message=str(exc))
                terminal_status = "failed"
        else:
            # The slot already has a run row. Advance past it only once that run has
            # reached a terminal state. An in-flight placeholder keeps the slot due
            # (so we never double-fire); a stale placeholder from a crashed worker is
            # abandoned after an hour so a single crash can't freeze the schedule.
            async with pools.ts.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT id::text, completed_at, triggered_at FROM schedule_runs "
                    "WHERE schedule_id = $1::uuid AND scheduled_for = $2",
                    schedule_id,
                    scheduled_for,
                )
            unfinished = existing is not None and existing["completed_at"] is None
            in_flight = (
                unfinished
                and existing["triggered_at"] is not None
                and existing["triggered_at"] > now_utc - timedelta(hours=1)
            )
            if in_flight:
                continue  # let the owning worker finish; leave next_run_at as-is
            if unfinished:
                logger.warning(
                    "report worker: retrying stale unfinished run for schedule %s slot %s",
                    schedule_id,
                    scheduled_for,
                )
                stale_run_id = existing["id"]
                try:
                    terminal_status = await execute_schedule_run(
                        pools,
                        schedule_row=schedule,
                        run_id=stale_run_id,
                        tz_name=tz_name,
                        email_client=email_client,
                        generate_report=generate_report,
                        default_from=default_from,
                        now_utc=now_utc,
                        scheduled_for=scheduled_for,
                    )
                    executed += 1
                except Exception as exc:  # noqa: BLE001 - one bad run must not stall the worker
                    logger.error(
                        "report worker: stale run retry failed schedule=%s: %s",
                        schedule_id,
                        exc,
                        exc_info=True,
                    )
                    async with pools.ts.acquire() as conn:
                        await finalize_run(
                            conn, stale_run_id, status="failed", error_message=str(exc)
                        )
                    terminal_status = "failed"

        # Advance next_run_at from the slot just handled (NOT from now), so a backlog
        # after an outage is worked off one missed slot per tick. Never fabricate a
        # fallback time on failure — leave the slot due so it is retried, not dropped.
        try:
            next_at = compute_next_run_at(
                scheduled_for,
                frequency=schedule["frequency"],
                time_of_day=schedule["time_of_day"],
                tz_name=tz_name,
                day_of_month=schedule["day_of_month"],
                day_of_week=schedule["day_of_week"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "report worker: reschedule failed schedule=%s; leaving slot due: %s",
                schedule_id,
                exc,
            )
            continue

        async with pools.ts.acquire() as conn:
            if terminal_status is not None:
                await conn.execute(
                    "UPDATE report_schedules SET next_run_at = $2, last_run_at = $3, "
                    "last_run_status = $4, updated_at = NOW() WHERE id = $1::uuid",
                    schedule_id,
                    next_at,
                    scheduled_for,
                    terminal_status,
                )
            else:
                # Slot already terminal (handled by another worker); only advance
                # if still on it.
                await conn.execute(
                    "UPDATE report_schedules SET next_run_at = $2, updated_at = NOW() "
                    "WHERE id = $1::uuid AND next_run_at = $3",
                    schedule_id,
                    next_at,
                    scheduled_for,
                )
    return executed


async def run_report_schedule_worker(
    pools: "DatabasePools",
    *,
    email_client: "EmailDeliveryClient",
    generate_report: GenerateReportFn,
    get_timezone: GetTimezoneFn,
    default_from: str,
    interval_s: int = _WORKER_INTERVAL_S,
) -> None:
    """Background task: every ``interval_s`` seconds, fire all due schedules."""
    logger.info("report schedule worker started (interval=%ss)", interval_s)
    while True:
        try:
            await _tick(
                pools,
                email_client=email_client,
                generate_report=generate_report,
                get_timezone=get_timezone,
                default_from=default_from,
            )
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            logger.error("report schedule worker: unhandled error: %s", exc, exc_info=True)
        await asyncio.sleep(interval_s)


__all__ = [
    "REPORT_DRAFT_ACTION_CLASS",
    "append_delivery_status_from_webhook",
    "compute_next_run_at",
    "deliver_pending_run",
    "execute_schedule_run",
    "delete_schedule",
    "fetch_schedule_row",
    "insert_schedule",
    "replace_recipients",
    "resolve_autonomy_mode",
    "run_report_schedule_worker",
    "serialize_run",
    "serialize_runs",
    "serialize_schedule",
    "serialize_schedules",
    "skip_pending_run",
    "update_schedule_fields",
    "previous_month_bounds",
]
