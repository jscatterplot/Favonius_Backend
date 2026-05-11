"""Monthly report-draft scheduler.

Emits one ``agent_actions`` row (action_class='report_draft') per depot
on the first day of each calendar month in the depot's local timezone.

Design decisions:
- Runs as a plain asyncio background task; no APScheduler dependency.
- Wakes every 15 minutes and checks which depots have entered the first
  calendar day of the month since the last check.  The 15-minute granularity
  means the action is created within 15 minutes of midnight on day 1 — well
  within any reasonable SLA.
- All inserts use ON CONFLICT DO NOTHING against the partial unique index
  ``idx_agent_actions_report_draft_period``, so re-runs (e.g. after a crash
  restart) are perfectly safe.
- A rejected action blocks re-emission for the same (depot, periodStart)
  because the unique index is unconditional on status.  The next month's
  period_start is different, so a new action is always emitted next cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_S: int = 900  # 15 minutes


def _prev_month_bounds(now_local: datetime) -> tuple[str, str, str]:
    """Return (period_start, period_end, month_label) for the previous calendar month.

    All date strings are YYYY-MM-DD in local time; month_label is 'Month YYYY'.
    """
    first_of_this_month = now_local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)

    period_start = first_of_prev.strftime("%Y-%m-%d")
    period_end = last_of_prev.strftime("%Y-%m-%d")
    month_label = first_of_prev.strftime("%B %Y")
    return period_start, period_end, month_label


async def _emit_for_depot(
    conn: asyncpg.Connection,
    depot_id: str,
    now_local: datetime,
) -> None:
    """Insert a report_draft agent_action for a depot if not already present."""
    period_start, period_end, month_label = _prev_month_bounds(now_local)
    title = f"Monthly consumption — {month_label} (by card)"
    summary = f"Generate {month_label} consumption report by RFID card"

    payload = {
        "kind": "monthly_consumption",
        "groupBy": "card",
        "periodStart": period_start,
        "periodEnd": period_end,
        "monthLabel": month_label,
        "title": title,
    }

    # created_at is 00:00 depot-local converted to UTC.
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    created_at_utc = midnight_local.astimezone(timezone.utc)

    await conn.execute(
        """
        INSERT INTO agent_actions
            (depot_id, agent_type, action_class, mode, status,
             summary, created_at, payload)
        VALUES
            ($1::uuid, 'reporting', 'report_draft', 'proposed', 'pending',
             $2, $3, $4::jsonb)
        ON CONFLICT DO NOTHING
        """,
        depot_id,
        summary,
        created_at_utc,
        json.dumps(payload),
    )


async def _check_and_emit(ts_pool: asyncpg.Pool) -> None:
    """Query all depots and emit report_draft actions where it is day 1 of the month."""
    async with ts_pool.acquire() as conn:
        # Fetch depot timezones from the static sites table via the ts pool.
        # In single-DB dev mode both pools point to the same instance.
        # In production the ts pool can still read `sites` because both
        # Supabase and TigerCloud share the same Postgres superuser or the
        # search_path includes the public schema from Supabase.
        #
        # Fallback: if sites is not accessible (schema not yet synced),
        # skip silently so we don't break the API on startup.
        try:
            rows = await conn.fetch(
                "SELECT id::text AS depot_id, timezone FROM sites WHERE timezone IS NOT NULL"
            )
        except asyncpg.UndefinedTableError:
            logger.warning("monthly_scheduler: sites table not found — skipping check")
            return
        except Exception as exc:  # pragma: no cover
            logger.warning("monthly_scheduler: failed to query sites: %s", exc)
            return

        for row in rows:
            depot_id = row["depot_id"]
            tz_name = row["timezone"]
            try:
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, KeyError):
                logger.warning(
                    "monthly_scheduler: unknown timezone %r for depot %s — skipping",
                    tz_name,
                    depot_id,
                )
                continue

            now_local = datetime.now(tz)
            if now_local.day != 1:
                continue  # Not day 1 — nothing to emit.

            try:
                await _emit_for_depot(conn, depot_id, now_local)
                logger.debug(
                    "monthly_scheduler: emitted/confirmed report_draft for depot %s (%s)",
                    depot_id,
                    tz_name,
                )
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "monthly_scheduler: error emitting action for depot %s: %s",
                    depot_id,
                    exc,
                    exc_info=True,
                )


async def run_monthly_scheduler(ts_pool: asyncpg.Pool) -> None:
    """Background task: check depots every 15 minutes and emit monthly drafts."""
    logger.info("monthly_scheduler: started (interval=%ds)", _CHECK_INTERVAL_S)
    while True:
        try:
            await _check_and_emit(ts_pool)
        except Exception as exc:  # pragma: no cover
            logger.error("monthly_scheduler: unhandled error: %s", exc, exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_S)
