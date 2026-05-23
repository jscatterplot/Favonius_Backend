"""Integration tests for scheduled reports against a real Postgres.

These exercise the migration 043 schema and the report_schedules repo/runtime
end-to-end (idempotency AC#7, bounce → lastDelivery AC#6, serialization
contract). They call the production repo functions directly and inject a fake
report generator + FakeEmailClient, so they validate the new SQL without needing
the full FastAPI app, Supabase `sites`, or the optimizer.

Requires DATABASE_URL / TEST_DATABASE_URL. Skips cleanly when no DB is reachable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.api import report_schedules as rs
from src.api.report_schedule_timing import compute_next_run_at, normalize_create_payload
from src.notifications.email_client import FakeEmailClient

pytestmark = [pytest.mark.integration, pytest.mark.database]

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


async def _apply_migrations(conn) -> None:
    """Apply migrations 033 (reports/agent_actions) and 043 idempotently."""
    for name in ("033_reports_agent_actions.sql", "043_report_schedules.sql"):
        sql = (_MIGRATIONS / name).read_text()
        await conn.execute(sql)


@pytest.fixture
async def schedules_db(pool):
    """A clean report-schedules schema with migrations applied."""
    async with pool.acquire() as conn:
        try:
            await _apply_migrations(conn)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"cannot apply report-schedule migrations: {exc}")
    yield pool
    # Best-effort cleanup of rows created by these tests.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM report_schedules WHERE name LIKE 'itest-%'")


def _depot_id() -> str:
    return str(uuid.uuid4())


def _create_input(name: str, autonomy_mode: str = "auto_silent") -> dict:
    return {
        "name": name,
        "kind": "monthly_consumption",
        "groupBy": "card",
        "frequency": "monthly",
        "dayOfMonth": 1,
        "timeOfDay": "06:00",
        "autonomyMode": autonomy_mode,
        "isActive": True,
        "recipients": [{"emailAddress": "manager@depot.example", "format": "pdf"}],
    }


async def _insert_schedule(pool, depot_id: str, name: str, autonomy_mode: str = "auto_silent"):
    norm = normalize_create_payload(_create_input(name, autonomy_mode))
    next_run_at = compute_next_run_at(
        datetime.now(timezone.utc),
        frequency=norm.frequency,
        time_of_day=norm.time_of_day,
        tz_name="Europe/Vilnius",
        day_of_month=norm.day_of_month,
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await rs.insert_schedule(
                conn, depot_id=depot_id, norm=norm, next_run_at=next_run_at, created_by=None
            )
            await rs.replace_recipients(conn, str(row["id"]), norm.recipients)
    return str(row["id"])


@pytest.mark.asyncio
async def test_migration_043_creates_tables(schedules_db):
    expected = {
        "report_schedules",
        "report_schedule_recipients",
        "schedule_runs",
        "schedule_run_deliveries",
        "autonomy_settings",
    }
    async with schedules_db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
            list(expected),
        )
    assert {r["table_name"] for r in rows} == expected


@pytest.mark.asyncio
async def test_empty_depot_serializes_to_empty_list(schedules_db):
    async with schedules_db.acquire() as conn:
        result = await rs.serialize_schedules(conn, _depot_id())
    assert result == []


@pytest.mark.asyncio
async def test_create_and_serialize_round_trip(schedules_db):
    depot_id = _depot_id()
    schedule_id = await _insert_schedule(schedules_db, depot_id, "itest-roundtrip")
    async with schedules_db.acquire() as conn:
        wire = await rs.serialize_schedule(conn, depot_id, schedule_id)

    assert wire is not None
    # Strict-null contract: every nullable field present.
    for key in ("nextRunAt", "lastRunAt", "lastRunStatus", "createdBy", "recipients"):
        assert key in wire
    assert wire["nextRunAt"] is not None and wire["nextRunAt"].endswith("Z")
    assert wire["timeOfDay"] == "06:00"
    assert wire["autonomyMode"] == "auto_silent"
    assert len(wire["recipients"]) == 1
    rec = wire["recipients"][0]
    assert rec["emailAddress"] == "manager@depot.example"
    assert rec["format"] == "pdf"
    assert rec["lastDelivery"] is None  # no run yet


@pytest.mark.asyncio
async def test_claim_run_is_idempotent_for_slot(schedules_db):
    """AC#7: concurrent ticks for the same slot do not double-fire."""
    depot_id = _depot_id()
    schedule_id = await _insert_schedule(schedules_db, depot_id, "itest-idempotent")
    slot = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)

    async with schedules_db.acquire() as conn:
        first = await rs.claim_run(conn, schedule_id, slot)
    async with schedules_db.acquire() as conn:
        second = await rs.claim_run(conn, schedule_id, slot)

    assert first is not None
    assert second is None  # the UNIQUE (schedule_id, scheduled_for) blocks the duplicate


@pytest.mark.asyncio
async def test_auto_silent_run_generates_report_and_delivers(schedules_db):
    """AC#4-ish: a run produces a report and records a delivery."""
    depot_id = _depot_id()
    schedule_id = await _insert_schedule(schedules_db, depot_id, "itest-autosilent")

    async with schedules_db.acquire() as conn:
        schedule_row = await rs.fetch_schedule_row(conn, depot_id, schedule_id)
        slot = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)
        run_id = await rs.claim_run(conn, schedule_id, slot)

    async def fake_generate(params, did):
        # Insert a real reports row so the delivery path can read it back.
        async with schedules_db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO reports (depot_id, title, kind, status, period_start, period_end, group_by, data)
                VALUES ($1::uuid, $2, 'monthly_consumption', 'draft', $3, $4, 'card', $5::jsonb)
                RETURNING id::text
                """,
                did,
                params["title"],
                datetime(2026, 5, 1, tzinfo=timezone.utc),
                datetime(2026, 6, 1, tzinfo=timezone.utc),
                '{"group_by": "card", "rows": [], "totals": null, "depot_name": "itest"}',
            )
        return row["id"]

    email = FakeEmailClient()

    class _Pools:
        ts = schedules_db
        static = schedules_db

    status = await rs.execute_schedule_run(
        _Pools(),
        schedule_row=schedule_row,
        run_id=run_id,
        tz_name="Europe/Vilnius",
        email_client=email,
        generate_report=fake_generate,
        default_from="reports@favonius.energy",
        now_utc=datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc),
    )

    assert status == "succeeded"
    assert len(email.sent) == 1
    async with schedules_db.acquire() as conn:
        run_wire = await rs.serialize_run(conn, run_id)
    assert run_wire["status"] == "succeeded"
    assert run_wire["reportId"] is not None
    assert len(run_wire["deliveries"]) == 1
    assert run_wire["deliveries"][0]["status"] == "sent"


@pytest.mark.asyncio
async def test_webhook_bounce_reflected_in_last_delivery(schedules_db):
    """AC#6: a provider bounce flips the recipient's lastDelivery to bounced."""
    depot_id = _depot_id()
    schedule_id = await _insert_schedule(schedules_db, depot_id, "itest-bounce")

    async with schedules_db.acquire() as conn:
        # Seed a run + a 'sent' delivery with a provider message id.
        run_id = await rs.claim_run(
            conn, schedule_id, datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)
        )
        recipient = await conn.fetchrow(
            "SELECT id FROM report_schedule_recipients WHERE schedule_id = $1::uuid",
            schedule_id,
        )
        await rs.insert_delivery(
            conn,
            run_id=run_id,
            recipient_id=str(recipient["id"]),
            email_address="manager@depot.example",
            fmt="pdf",
            status="sent",
            provider_message_id="provider-msg-1",
            error=None,
        )

        # Provider webhook: the message bounced.
        appended = await rs.append_delivery_status_from_webhook(
            conn,
            provider_message_id="provider-msg-1",
            provider_status="bounced",
            detail={"reason": "mailbox full"},
        )
        assert appended is True

        wire = await rs.serialize_schedule(conn, depot_id, schedule_id)

    last = wire["recipients"][0]["lastDelivery"]
    assert last is not None
    assert last["status"] == "bounced"


@pytest.mark.asyncio
async def test_cadence_check_constraint_rejects_bad_combo(schedules_db):
    """The DB cadence CHECK rejects weekly+dayOfMonth (defense in depth)."""
    import asyncpg

    async with schedules_db.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO report_schedules
                    (depot_id, name, kind, frequency, day_of_month, day_of_week,
                     time_of_day, autonomy_mode)
                VALUES ($1::uuid, 'itest-bad', 'weekly_ops', 'weekly', 5, 1, '06:00', 'auto_silent')
                """,
                _depot_id(),
            )
