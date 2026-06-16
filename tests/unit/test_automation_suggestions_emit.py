"""Async tests for the automation-suggestion persistence + dedup + wrapper.

Uses fake asyncpg-like connections/pools and monkeypatches the lazily-imported
helpers (``resolve_autonomy_mode``, ``get_user_email``) so no real DB is needed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from src.api.agent import automation_suggestions as autosug
from src.api.agent.automation_suggestions import (
    Suggestion,
    SuggestionConfig,
    SuggestionSignature,
    _action_payload,
    _active_schedule_exists,
    _detect_and_emit,
    _recent_rejection_exists,
    emit_schedule_suggestion_action,
    maybe_emit_automation_suggestion,
    resolve_emit_depot,
)

NOW = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeConn:
    """asyncpg-like connection branching on query substrings."""

    def __init__(self, *, existing=False, rejected=False, insert_returns=True):
        self.existing = existing
        self.rejected = rejected
        self.insert_returns = insert_returns
        self.inserts: list[tuple] = []
        self.calls: list[tuple] = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "INSERT INTO agent_actions" in query:
            self.inserts.append(args)
            return {"id": "new-id"} if self.insert_returns else None
        if "FROM report_schedules" in query:
            return {"x": 1} if self.existing else None
        if "status = 'rejected'" in query:
            return {"x": 1} if self.rejected else None
        return None


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn, rows):
        self.conn = conn
        self.rows = rows
        self.fetch_calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self.rows

    def acquire(self):
        return _Acquire(self.conn)


def _auth(n_depots=1):
    return SimpleNamespace(user_id=uuid4(), visible_depot_ids=[uuid4() for _ in range(n_depots)])


def _make_suggestion(group="card", cadence="weekly", email="ops@example.com") -> Suggestion:
    sig = SuggestionSignature("consumption_by_user", group, cadence)
    si = autosug._schedule_input_from_signature(sig, SuggestionConfig(), email)
    return Suggestion(
        signature=sig,
        schedule_input=si,
        summary="Should I set this up?",
        occurrence_count=3,
        lookback_days=30,
        sample_messages=["energy by card"],
        first_seen="2026-05-01T00:00:00+00:00",
        last_seen="2026-05-20T00:00:00+00:00",
    )


def _weekly_card_runs(n=3):
    runs = []
    for i in range(n):
        plan = {
            "intent": "consumption_by_user",
            "subjects": [{"kind": "driver", "text": "John"}],
            "time_window": {"kind": "relative", "relative": "last_week"},
            "group_by": [],
            "depot_wide": False,
        }
        runs.append(
            {
                "run_id": str(uuid4()),
                "user_message": f"energy by John {i}",
                "final_intent": "consumption_by_user",
                "status": "success",
                "created_at": NOW - timedelta(days=1 + 7 * i),
                "steps_json": [{"name": "extract_plan", "payload": plan}],
            }
        )
    return runs


async def _async_const(value):
    return value


# ── _action_payload + emit helper ─────────────────────────────────────────────


def test_action_payload_shape():
    s = _make_suggestion()
    p = _action_payload(s)
    assert p["signatureKey"] == s.signature.key()
    assert p["signature"]["key"] == s.signature.key()
    assert p["signature"]["cadence"] == "weekly"
    assert p["scheduleInput"]["kind"] == "monthly_consumption"
    assert p["evidence"]["occurrenceCount"] == 3
    assert p["evidence"]["sampleMessages"] == ["energy by card"]


def test_emit_inserts_pending_row():
    conn = FakeConn(insert_returns=True)
    s = _make_suggestion()
    ok = asyncio.run(
        emit_schedule_suggestion_action(conn, depot_id="d1", mode="proposed", suggestion=s)
    )
    assert ok is True
    assert len(conn.inserts) == 1
    args = conn.inserts[0]
    # (depot_id, agent_type, action_class, mode, summary, payload_json)
    assert args[0] == "d1"
    assert args[1] == "automation"
    assert args[2] == "schedule_suggestion"
    assert args[3] == "proposed"
    payload = json.loads(args[-1])
    assert payload["signatureKey"] == "consumption_by_user|card|weekly"


def test_emit_returns_false_on_conflict():
    conn = FakeConn(insert_returns=False)
    ok = asyncio.run(
        emit_schedule_suggestion_action(
            conn, depot_id="d1", mode="proposed", suggestion=_make_suggestion()
        )
    )
    assert ok is False


# ── resolve_emit_depot + dedup predicates ─────────────────────────────────────


def test_resolve_emit_depot():
    one = uuid4()
    assert resolve_emit_depot(SimpleNamespace(visible_depot_ids=[one])) == one
    assert resolve_emit_depot(SimpleNamespace(visible_depot_ids=[])) is None
    assert resolve_emit_depot(SimpleNamespace(visible_depot_ids=[uuid4(), uuid4()])) is None


def test_active_schedule_exists():
    assert (
        asyncio.run(_active_schedule_exists(FakeConn(existing=True), "d", "card", "weekly")) is True
    )
    assert (
        asyncio.run(_active_schedule_exists(FakeConn(existing=False), "d", "card", "weekly"))
        is False
    )


def test_recent_rejection_exists():
    assert asyncio.run(_recent_rejection_exists(FakeConn(rejected=True), "d", "k", 60)) is True
    assert asyncio.run(_recent_rejection_exists(FakeConn(rejected=False), "d", "k", 60)) is False


def test_recent_rejection_zero_cooldown_skips_query():
    conn = FakeConn(rejected=True)
    assert asyncio.run(_recent_rejection_exists(conn, "d", "k", 0)) is False
    assert conn.calls == []  # no query issued when cooldown disabled


# ── _detect_and_emit orchestration ────────────────────────────────────────────


def _patch_deps(monkeypatch, *, autonomy="proposed", email="ops@example.com"):
    monkeypatch.setattr(
        "src.api.report_schedules.resolve_autonomy_mode",
        lambda *a, **k: _async_const(autonomy),
    )
    monkeypatch.setattr("src.security.auth.get_user_email", lambda payload: email)


def test_detect_and_emit_happy_path(monkeypatch):
    _patch_deps(monkeypatch)
    conn = FakeConn()
    pool = FakePool(conn, rows=_weekly_card_runs())
    asyncio.run(_detect_and_emit(pool, _auth(1), {}, SuggestionConfig(), NOW))
    assert len(conn.inserts) == 1
    payload = json.loads(conn.inserts[0][-1])
    assert payload["signatureKey"] == "consumption_by_user|card|weekly"


def test_detect_and_emit_suppressed_by_existing_schedule(monkeypatch):
    _patch_deps(monkeypatch)
    conn = FakeConn(existing=True)
    pool = FakePool(conn, rows=_weekly_card_runs())
    asyncio.run(_detect_and_emit(pool, _auth(1), {}, SuggestionConfig(), NOW))
    assert conn.inserts == []


def test_detect_and_emit_suppressed_by_rejected_cooldown(monkeypatch):
    _patch_deps(monkeypatch)
    conn = FakeConn(rejected=True)
    pool = FakePool(conn, rows=_weekly_card_runs())
    asyncio.run(_detect_and_emit(pool, _auth(1), {}, SuggestionConfig(), NOW))
    assert conn.inserts == []


def test_detect_and_emit_suppressed_by_autonomy_shadow(monkeypatch):
    _patch_deps(monkeypatch, autonomy="shadow")
    conn = FakeConn()
    pool = FakePool(conn, rows=_weekly_card_runs())
    asyncio.run(_detect_and_emit(pool, _auth(1), {}, SuggestionConfig(), NOW))
    assert conn.inserts == []


def test_detect_and_emit_depot_unresolvable_skips_fetch(monkeypatch):
    _patch_deps(monkeypatch)
    conn = FakeConn()
    pool = FakePool(conn, rows=_weekly_card_runs())
    asyncio.run(_detect_and_emit(pool, _auth(2), {}, SuggestionConfig(), NOW))
    assert pool.fetch_calls == []  # returned before mining history
    assert conn.inserts == []


def test_detect_and_emit_no_suggestion_does_not_acquire(monkeypatch):
    _patch_deps(monkeypatch)
    conn = FakeConn()
    pool = FakePool(conn, rows=_weekly_card_runs(n=1))  # below threshold
    asyncio.run(_detect_and_emit(pool, _auth(1), {}, SuggestionConfig(), NOW))
    assert conn.inserts == []
    assert conn.calls == []  # acquire/dedup never entered


# ── maybe_emit_automation_suggestion (flag + best-effort) ──────────────────────


def test_wrapper_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_AUTOMATION_SUGGESTIONS_ENABLED", raising=False)
    called = {"v": False}

    async def _boom(*a, **k):
        called["v"] = True

    monkeypatch.setattr(autosug, "_detect_and_emit", _boom)
    asyncio.run(maybe_emit_automation_suggestion(ts_pool=object(), auth=_auth(1), token_payload={}))
    assert called["v"] is False


def test_wrapper_invokes_detection_when_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_SUGGESTIONS_ENABLED", "true")
    seen = {}

    async def _rec(ts_pool, auth, token_payload, config, now):
        seen["called"] = True

    monkeypatch.setattr(autosug, "_detect_and_emit", _rec)
    asyncio.run(maybe_emit_automation_suggestion(ts_pool="P", auth=_auth(1), token_payload={}))
    assert seen.get("called") is True


def test_wrapper_swallows_errors(monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_SUGGESTIONS_ENABLED", "true")

    async def _raiser(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(autosug, "_detect_and_emit", _raiser)
    # Must not raise — best-effort.
    asyncio.run(maybe_emit_automation_suggestion(ts_pool="P", auth=_auth(1), token_payload={}))


def test_wrapper_times_out_without_raising(monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_SUGGESTIONS_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTOMATION_TIMEOUT_S", "0.05")

    async def _slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(autosug, "_detect_and_emit", _slow)
    asyncio.run(maybe_emit_automation_suggestion(ts_pool="P", auth=_auth(1), token_payload={}))


def test_wrapper_swallows_pre_detection_setup_errors(monkeypatch):
    """A failure in the pre-detection setup (config/import/now) must not escape.

    Regression: those statements used to run outside the try/except, so an
    exception would propagate into run_turn's outer handler and 502 a turn that
    already succeeded.
    """
    monkeypatch.setenv("AGENT_AUTOMATION_SUGGESTIONS_ENABLED", "true")

    def _boom():
        raise RuntimeError("from_env exploded")

    monkeypatch.setattr(autosug.SuggestionConfig, "from_env", staticmethod(_boom))
    # No config passed → from_env() is invoked inside the wrapper; must be swallowed.
    asyncio.run(maybe_emit_automation_suggestion(ts_pool="P", auth=_auth(1), token_payload={}))
