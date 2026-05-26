"""Unit tests for report_schedules: serializers, run execution (autonomy
gating), webhook append, and approval/rejection helpers.

Driven against an in-memory fake asyncpg pool so they run without a DB. Async
tests use asyncio.run to avoid coupling to a pytest-asyncio version.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone

from src.api import report_schedules as rs
from src.api.report_schedule_timing import compute_next_run_at
from src.notifications.email_client import DeliveryResult, FakeEmailClient


def run(coro):
    return asyncio.run(coro)


def _dt(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# ── Fake asyncpg pool/conn ────────────────────────────────────────────────────


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, store: dict):
        self.store = store
        self.executes: list[tuple[str, tuple]] = []
        store["executes"] = self.executes

    async def fetchrow(self, query: str, *args):
        if "FROM agent_autonomy_settings" in query:
            return self.store.get("autonomy_row")
        if "FROM reports WHERE id" in query:
            return self.store.get("report_row")
        if "FROM schedule_runs WHERE id" in query:
            return self.store.get("run_row")
        if "WHERE provider_message_id" in query:
            return self.store.get("origin_delivery")
        if "INSERT INTO schedule_runs" in query:
            return self.store.get("claim_result")
        if "INSERT INTO agent_actions" in query:
            self.executes.append((query, args))
            if self.store.get("report_draft_conflict"):
                return None
            return {"id": "action-1"}
        if "UPDATE schedule_runs SET status = 'succeeded'" in query:
            run_row = self.store.get("run_row")
            if run_row is not None:
                run_row["status"] = "succeeded"
            return {"schedule_id": "11111111-1111-1111-1111-111111111111"}
        return None

    async def fetchval(self, query: str, *args):
        if "completed_at FROM schedule_runs" in query:
            return self.store.get("completed_at")
        return None

    async def fetch(self, query: str, *args):
        if "FROM report_schedules" in query and "is_active" in query:
            return self.store.get("due", [])
        if "FROM report_schedule_recipients" in query:
            return self.store.get("recipient_rows", [])
        if "FROM schedule_run_deliveries" in query:
            return self.store.get("delivery_rows", [])
        return []

    async def execute(self, query: str, *args):
        self.executes.append((query, args))
        if "UPDATE schedule_runs" in query and "SET status" in query:
            run_row = self.store.get("run_row")
            if run_row is not None and len(args) >= 2:
                run_row["status"] = args[1]
                if len(args) >= 5:
                    run_row["error_message"] = args[4]
        return "OK"

    def transaction(self):
        return _FakeTx()


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class FakePools:
    def __init__(self, conn):
        self.ts = FakePool(conn)
        self.static = FakePool(conn)


def _finalize_status(store) -> str | None:
    for query, args in store["executes"]:
        if "UPDATE schedule_runs" in query and "SET status" in query:
            return args[1]
    return None


def _has_execute(store, needle: str) -> bool:
    return any(needle in q for q, _ in store["executes"])


def _schedule_row(autonomy_mode="auto_silent", kind="monthly_consumption", group_by="card"):
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "depot_id": "22222222-2222-2222-2222-222222222222",
        "name": "Monthly electricity consumption",
        "kind": kind,
        "group_by": group_by,
        "frequency": "monthly",
        "day_of_month": 1,
        "day_of_week": None,
        "autonomy_mode": autonomy_mode,
    }


def _report_row():
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "depot_id": "22222222-2222-2222-2222-222222222222",
        "title": "Monthly electricity consumption — 2026-05-01 to 2026-05-31",
        "kind": "monthly_consumption",
        "group_by": "card",
        "period_start": _dt(2026, 5, 1),
        "period_end": _dt(2026, 5, 31),
        "data": {"group_by": "card", "rows": [], "totals": None, "depot_name": "HRX"},
    }


def _recipient_rows():
    return [
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "email_address": "manager@depot.example",
            "format": "pdf",
        }
    ]


async def _run_execute(store, *, autonomy_mode, email_client=None, generated="report-xyz"):
    conn = FakeConn(store)
    pools = FakePools(conn)
    calls = {"generate": 0}

    async def fake_generate(params, depot_id):
        calls["generate"] += 1
        store["last_generate_params"] = params
        return generated

    status = await rs.execute_schedule_run(
        pools,
        schedule_row=_schedule_row(autonomy_mode=autonomy_mode),
        run_id="run-1",
        tz_name="UTC",
        email_client=email_client or FakeEmailClient(),
        generate_report=fake_generate,
        default_from="reports@favonius.energy",
        now_utc=_dt(2026, 6, 1, 3, 0),
    )
    store["generate_calls"] = calls["generate"]
    return status


# ── Autonomy gating ───────────────────────────────────────────────────────────


def test_shadow_skips_without_report_or_delivery():
    store: dict = {"autonomy_row": None}
    status = run(_run_execute(store, autonomy_mode="shadow"))
    assert status == "skipped"
    assert store["generate_calls"] == 0
    assert _finalize_status(store) == "skipped"
    assert not _has_execute(store, "INSERT INTO schedule_run_deliveries")
    assert not _has_execute(store, "INSERT INTO agent_actions")


def test_proposed_generates_report_emits_action_and_pends():
    store: dict = {"autonomy_row": None}
    status = run(_run_execute(store, autonomy_mode="proposed"))
    assert status == "pending_approval"
    assert store["generate_calls"] == 1
    assert _has_execute(store, "INSERT INTO agent_actions")
    assert _finalize_status(store) == "pending_approval"
    # proposed must NOT deliver.
    assert not _has_execute(store, "INSERT INTO schedule_run_deliveries")


def test_proposed_duplicate_period_skips_run_without_orphan_pending():
    store: dict = {"autonomy_row": None, "report_draft_conflict": True}
    status = run(_run_execute(store, autonomy_mode="proposed"))
    assert status == "skipped"
    assert store["generate_calls"] == 1
    assert _finalize_status(store) == "skipped"
    assert not _has_execute(store, "INSERT INTO schedule_run_deliveries")


def test_auto_silent_delivers_without_agent_action():
    store: dict = {
        "autonomy_row": None,
        "report_row": _report_row(),
        "recipient_rows": _recipient_rows(),
    }
    email = FakeEmailClient()
    status = run(_run_execute(store, autonomy_mode="auto_silent", email_client=email))
    assert status == "succeeded"
    assert store["generate_calls"] == 1
    assert len(email.sent) == 1
    assert email.sent[0].to == "manager@depot.example"
    assert len(email.sent[0].attachments) == 1
    assert email.sent[0].attachments[0].filename.endswith(".pdf")
    assert _has_execute(store, "INSERT INTO schedule_run_deliveries")
    assert not _has_execute(store, "INSERT INTO agent_actions")
    assert _finalize_status(store) == "succeeded"


def test_auto_notify_delivers_and_emits_informational_action():
    store: dict = {
        "autonomy_row": None,
        "report_row": _report_row(),
        "recipient_rows": _recipient_rows(),
    }
    email = FakeEmailClient()
    status = run(_run_execute(store, autonomy_mode="auto_notify", email_client=email))
    assert status == "succeeded"
    assert len(email.sent) == 1
    assert _has_execute(store, "INSERT INTO schedule_run_deliveries")
    assert _has_execute(store, "INSERT INTO agent_actions")
    assert _finalize_status(store) == "succeeded"


def test_agent_autonomy_settings_override_wins_over_schedule_mode():
    # Schedule says auto_silent, but the agent_autonomy_settings row forces shadow.
    store: dict = {"autonomy_row": {"level": "shadow"}}
    status = run(_run_execute(store, autonomy_mode="auto_silent"))
    assert status == "skipped"
    assert store["generate_calls"] == 0


def test_report_generation_failure_marks_run_failed():
    store: dict = {"autonomy_row": None}
    conn = FakeConn(store)
    pools = FakePools(conn)

    async def boom(params, depot_id):
        raise RuntimeError("solver down")

    status = run(
        rs.execute_schedule_run(
            pools,
            schedule_row=_schedule_row(autonomy_mode="auto_silent"),
            run_id="run-1",
            tz_name="UTC",
            email_client=FakeEmailClient(),
            generate_report=boom,
            default_from="reports@favonius.energy",
            now_utc=_dt(2026, 6, 1, 3, 0),
        )
    )
    assert status == "failed"
    assert _finalize_status(store) == "failed"


def test_deliver_pending_run_keeps_pending_when_send_fails():
    store: dict = {
        "run_row": {
            "id": "run-1",
            "schedule_id": "11111111-1111-1111-1111-111111111111",
            "report_id": "33333333-3333-3333-3333-333333333333",
            "triggered_at": _dt(2026, 6, 1, 3, 0),
            "completed_at": None,
            "status": "pending_approval",
            "error_message": None,
        },
        "report_row": _report_row(),
        "recipient_rows": _recipient_rows(),
    }
    conn = FakeConn(store)
    pools = FakePools(conn)

    def failing_script(message, counter):
        return DeliveryResult(status="failed", provider_message_id=None, detail={"error": "boom"})

    email = FakeEmailClient(script=failing_script)

    async def _deliver():
        wire, ok = await rs.deliver_pending_run(
            pools,
            run_id="run-1",
            email_client=email,
            default_from="reports@favonius.energy",
        )
        return wire, ok

    wire, ok = run(_deliver())
    assert ok is False
    assert wire is not None
    assert wire["status"] == "pending_approval"
    assert not any(
        "SET status = 'succeeded'" in q for q, _ in store["executes"] if "schedule_runs" in q
    )


def test_deliver_pending_run_marks_succeeded_when_all_sent():
    store: dict = {
        "run_row": {
            "id": "run-1",
            "schedule_id": "11111111-1111-1111-1111-111111111111",
            "report_id": "33333333-3333-3333-3333-333333333333",
            "triggered_at": _dt(2026, 6, 1, 3, 0),
            "completed_at": None,
            "status": "pending_approval",
            "error_message": None,
        },
        "report_row": _report_row(),
        "recipient_rows": _recipient_rows(),
    }
    conn = FakeConn(store)
    pools = FakePools(conn)
    email = FakeEmailClient()

    async def _deliver():
        return await rs.deliver_pending_run(
            pools,
            run_id="run-1",
            email_client=email,
            default_from="reports@favonius.energy",
        )

    wire, ok = run(_deliver())
    assert ok is True
    assert wire is not None
    assert wire["status"] == "succeeded"
    assert len(email.sent) == 1


def test_delivery_failure_recorded_per_recipient():
    store: dict = {
        "autonomy_row": None,
        "report_row": _report_row(),
        "recipient_rows": _recipient_rows(),
    }

    def failing_script(message, counter):
        return DeliveryResult(status="failed", provider_message_id=None, detail={"error": "boom"})

    email = FakeEmailClient(script=failing_script)
    status = run(_run_execute(store, autonomy_mode="auto_silent", email_client=email))
    # The run still 'succeeded' (report generated, attempt logged); the failure
    # lives on the delivery row.
    assert status == "succeeded"
    delivery_inserts = [
        args for q, args in store["executes"] if "INSERT INTO schedule_run_deliveries" in q
    ]
    assert len(delivery_inserts) == 1
    # insert_delivery args: (run_id, recipient_id, email, fmt, status, provider_message_id, error)
    assert delivery_inserts[0][4] == "failed"


# ── Webhook append ────────────────────────────────────────────────────────────


def test_webhook_append_inserts_mapped_status():
    store: dict = {
        "origin_delivery": {
            "run_id": "run-1",
            "recipient_id": "rcpt-1",
            "email_address": "manager@depot.example",
            "format": "pdf",
            "status": "sent",
        }
    }
    conn = FakeConn(store)
    found = run(
        rs.append_delivery_status_from_webhook(
            conn,
            provider_message_id="msg-123",
            provider_status="bounced",
            detail={"reason": "mailbox full"},
        )
    )
    assert found is True
    inserts = [args for q, args in store["executes"] if "INSERT INTO schedule_run_deliveries" in q]
    assert len(inserts) == 1
    assert inserts[0][4] == "bounced"  # status mapped + stored


def test_webhook_append_complaint_maps_to_suppressed():
    store: dict = {
        "origin_delivery": {
            "run_id": "run-1",
            "recipient_id": "rcpt-1",
            "email_address": "m@d.example",
            "format": "csv",
            "status": "sent",
        }
    }
    conn = FakeConn(store)
    run(
        rs.append_delivery_status_from_webhook(
            conn, provider_message_id="m", provider_status="complained", detail=None
        )
    )
    inserts = [args for q, args in store["executes"] if "INSERT INTO schedule_run_deliveries" in q]
    assert inserts[0][4] == "suppressed"


def test_webhook_append_unknown_message_is_noop():
    store: dict = {"origin_delivery": None}
    conn = FakeConn(store)
    found = run(
        rs.append_delivery_status_from_webhook(
            conn, provider_message_id="nope", provider_status="bounced", detail=None
        )
    )
    assert found is False


def test_webhook_late_sent_after_bounce_is_ignored():
    # Out-of-order: a 'delivered'/'sent' event arriving after a terminal bounce
    # must NOT append (which would mask the bounce in lastDelivery).
    store: dict = {
        "origin_delivery": {
            "run_id": "run-1",
            "recipient_id": "rcpt-1",
            "email_address": "m@d.example",
            "format": "pdf",
            "status": "bounced",
        }
    }
    conn = FakeConn(store)
    found = run(
        rs.append_delivery_status_from_webhook(
            conn, provider_message_id="msg-1", provider_status="delivered", detail=None
        )
    )
    assert found is True  # message recognized…
    inserts = [args for q, args in store["executes"] if "INSERT INTO schedule_run_deliveries" in q]
    assert inserts == []  # …but the regressive 'sent' row was dropped


def test_webhook_second_bounce_after_bounce_still_appends():
    # A terminal-negative following a terminal-negative is fine (keep latest).
    store: dict = {
        "origin_delivery": {
            "run_id": "run-1",
            "recipient_id": "rcpt-1",
            "email_address": "m@d.example",
            "format": "pdf",
            "status": "bounced",
        }
    }
    conn = FakeConn(store)
    run(
        rs.append_delivery_status_from_webhook(
            conn, provider_message_id="msg-1", provider_status="complained", detail=None
        )
    )
    inserts = [args for q, args in store["executes"] if "INSERT INTO schedule_run_deliveries" in q]
    assert inserts[0][4] == "suppressed"


# ── Serializers (null-presence contract) ──────────────────────────────────────


def test_schedule_to_wire_includes_all_nullable_fields():
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "depot_id": "22222222-2222-2222-2222-222222222222",
        "name": "Monthly",
        "kind": "monthly_consumption",
        "group_by": "card",
        "frequency": "monthly",
        "day_of_month": 1,
        "day_of_week": None,
        "time_of_day": __import__("datetime").time(6, 0),
        "autonomy_mode": "auto_silent",
        "is_active": True,
        "next_run_at": _dt(2026, 6, 1, 3, 0),
        "last_run_at": None,
        "last_run_status": None,
        "created_at": _dt(2026, 5, 20, 9, 0),
        "created_by": None,
        "updated_at": _dt(2026, 5, 20, 9, 0),
    }
    wire = rs.schedule_to_wire(row, recipients=[])
    # All strict-null fields must be present (Zod fails on missing keys).
    for key in ("nextRunAt", "lastRunAt", "lastRunStatus", "createdBy", "recipients"):
        assert key in wire
    assert wire["recipients"] == []
    assert wire["nextRunAt"] == "2026-06-01T03:00:00Z"
    assert wire["timeOfDay"] == "06:00"
    assert wire["lastRunAt"] is None
    assert wire["dayOfWeek"] is None


def test_recipient_to_wire_last_delivery_present_when_none():
    row = {
        "id": "44444444-4444-4444-4444-444444444444",
        "email_address": "m@d.example",
        "format": "pdf",
    }
    wire = rs.recipient_to_wire(row, last_delivery=None)
    assert wire["lastDelivery"] is None
    assert wire["recipientId"] == "44444444-4444-4444-4444-444444444444"


def test_run_to_wire_shapes_deliveries_and_nulls():
    row = {
        "id": "55555555-5555-5555-5555-555555555555",
        "schedule_id": "11111111-1111-1111-1111-111111111111",
        "report_id": None,
        "triggered_at": _dt(2026, 6, 1, 3, 0),
        "completed_at": None,
        "status": "failed",
        "error_message": "boom",
    }
    wire = rs.run_to_wire(row, deliveries=[])
    assert wire["reportId"] is None
    assert wire["completedAt"] is None
    assert wire["deliveries"] == []
    assert wire["status"] == "failed"
    assert wire["errorMessage"] == "boom"


def test_delivery_to_wire_shape():
    row = {
        "recipient_id": "44444444-4444-4444-4444-444444444444",
        "email_address": "m@d.example",
        "format": "pdf",
        "status": "bounced",
        "attempted_at": _dt(2026, 6, 1, 3, 5),
        "provider_message_id": "msg-1",
        "error": None,
    }
    wire = rs.delivery_to_wire(row)
    assert wire == {
        "recipientId": "44444444-4444-4444-4444-444444444444",
        "emailAddress": "m@d.example",
        "format": "pdf",
        "status": "bounced",
        "attemptedAt": "2026-06-01T03:05:00Z",
        "providerMessageId": "msg-1",
        "error": None,
    }


# ── Worker catch-up (overdue slots) ───────────────────────────────────────────


def test_tick_advances_from_fired_slot_not_now():
    # After an outage, the worker must walk forward from the slot it just fired
    # (catching up missed periods one tick at a time), NOT jump to next-from-now.
    scheduled_for = _dt(2026, 6, 1, 6, 0)  # the overdue monthly slot (UTC)
    now = _dt(2026, 8, 15, 12, 0)  # two months later
    due_row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "depot_id": "22222222-2222-2222-2222-222222222222",
        "name": "Monthly",
        "kind": "monthly_consumption",
        "group_by": "card",
        "frequency": "monthly",
        "day_of_month": 1,
        "day_of_week": None,
        "time_of_day": time(6, 0),
        "autonomy_mode": "shadow",  # quick skip — no report/delivery needed
        "next_run_at": scheduled_for,
    }
    store: dict = {"due": [due_row], "autonomy_row": None, "claim_result": {"id": "run-1"}}
    conn = FakeConn(store)
    pools = FakePools(conn)

    async def gettz(depot_id):
        return "UTC"

    async def fake_generate(params, depot_id):  # pragma: no cover - shadow never calls
        return "report-x"

    run(
        rs._tick(
            pools,
            email_client=FakeEmailClient(),
            generate_report=fake_generate,
            get_timezone=gettz,
            default_from="reports@favonius.energy",
            now_utc=now,
        )
    )

    updates = [
        args
        for q, args in store["executes"]
        if "UPDATE report_schedules" in q and "next_run_at" in q
    ]
    assert updates, "expected a report_schedules reschedule UPDATE"
    next_at = updates[0][1]
    expected = compute_next_run_at(
        scheduled_for, frequency="monthly", time_of_day=time(6, 0), tz_name="UTC", day_of_month=1
    )
    assert next_at == expected == _dt(2026, 7, 1, 6, 0)  # July, not September


def test_tick_tz_failure_leaves_slot_due_for_retry():
    # When the depot timezone can't be resolved, the slot is left due (next_run_at
    # untouched, no run claimed) so a transient outage is retried next tick rather
    # than the slot being advanced/dropped.
    scheduled_for = _dt(2026, 6, 1, 6, 0)
    now = _dt(2026, 6, 1, 12, 0)
    due_row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "depot_id": "22222222-2222-2222-2222-222222222222",
        "name": "Monthly",
        "kind": "monthly_consumption",
        "group_by": "card",
        "frequency": "monthly",
        "day_of_month": 1,
        "day_of_week": None,
        "time_of_day": time(6, 0),
        "autonomy_mode": "auto_silent",
        "next_run_at": scheduled_for,
    }
    store: dict = {"due": [due_row], "claim_result": {"id": "run-1"}}
    conn = FakeConn(store)
    pools = FakePools(conn)

    async def gettz(depot_id):
        raise RuntimeError("sites unreachable")

    async def fake_generate(params, depot_id):  # pragma: no cover - never reached
        return "report-x"

    run(
        rs._tick(
            pools,
            email_client=FakeEmailClient(),
            generate_report=fake_generate,
            get_timezone=gettz,
            default_from="reports@favonius.energy",
            now_utc=now,
        )
    )

    # No run was claimed/finalized and next_run_at was NOT advanced — the slot
    # stays due so the next tick retries it.
    assert _finalize_status(store) is None
    assert not _has_execute(store, "INSERT INTO schedule_runs")
    assert not _has_execute(store, "UPDATE report_schedules")


def test_wait_for_run_finalized_returns_when_completed():
    # Regression guard: exercises wait_for_run_finalized (uses time.monotonic),
    # which would NameError if `import time` were missing from the module.
    store: dict = {"completed_at": _dt(2026, 6, 1, 3, 5)}
    conn = FakeConn(store)
    pools = FakePools(conn)
    # Returns promptly because completed_at is already set.
    run(rs.wait_for_run_finalized(pools, "run-1", poll_interval_s=0.01, timeout_s=1.0))
