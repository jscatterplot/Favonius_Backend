"""Unit tests for the charger-log reconciler.

Uses a fake asyncpg connection that returns canned rows for each query
the reconciler issues. Real SQL execution is covered by the integration
test; here we focus on the source-enum decision logic and the
``_charger_total_kwh`` helper.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.core.reconciliation.session_log_reconciliation import (
    ReconciliationResult,
    _charger_total_kwh,
    reconcile_session_log,
    write_session_log_reconciliation,
)


_NOW = datetime(2024, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Fake pool plumbing
# ---------------------------------------------------------------------------


class _FakeConn:
    """Fetch helpers return queued rows in the order calls come in."""

    def __init__(
        self,
        *,
        session_row=None,
        import_row=None,
        charger_row=None,
        energy_row=None,
    ):
        self._session_row = session_row
        self._import_row = import_row
        self._charger_row = charger_row
        self._energy_row = energy_row
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        # The reconciler queries in this order:
        #   1. charging_sessions row
        #   2. charger_log_imports row
        #   3. charger_session_log_entries aggregate
        #   4. (from _our_energy_kwh) telemetry energy
        if "FROM charging_sessions" in sql:
            return self._session_row
        if "FROM charger_log_imports" in sql:
            return self._import_row
        if "FROM charger_session_log_entries" in sql:
            return self._charger_row
        if "FROM telemetry" in sql or "samples" in sql:
            return self._energy_row
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


def _make_session_row(**overrides):
    base = {
        "session_id": uuid4(),
        "station_id": "OCPP-01",
        "connector_id": 1,
        "transaction_id": 12345,
        "vehicle_id": uuid4(),
        "start_time": _NOW,
        "end_time": _LATER,
        "energy_delivered_kwh": 5.5,
    }
    base.update(overrides)
    return base


def _make_charger_aggregate(**overrides):
    base = {
        "sample_count": 4,
        "min_time": _NOW,
        "max_time": _LATER,
        "max_energy_kwh": 5.55,
        "min_energy_kwh": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _charger_total_kwh helper
# ---------------------------------------------------------------------------


class TestChargerTotalKwh:
    def test_max_minus_min_when_both_present(self):
        assert _charger_total_kwh(max_energy_kwh=10.0, min_energy_kwh=2.0) == 8.0

    def test_returns_max_when_min_missing(self):
        assert _charger_total_kwh(max_energy_kwh=8.0, min_energy_kwh=None) == 8.0

    def test_returns_none_when_max_missing(self):
        assert _charger_total_kwh(max_energy_kwh=None, min_energy_kwh=0.0) is None

    def test_returns_none_when_max_is_zero(self):
        assert _charger_total_kwh(max_energy_kwh=0.0, min_energy_kwh=0.0) is None

    def test_falls_back_to_max_when_delta_non_positive(self):
        # Single-sample case: max == min, delta = 0, fall back to max.
        assert _charger_total_kwh(max_energy_kwh=5.0, min_energy_kwh=5.0) == 5.0
        # Non-monotonic case (meter glitch): max < min, fall back to max.
        assert _charger_total_kwh(max_energy_kwh=4.0, min_energy_kwh=6.0) == 4.0


# ---------------------------------------------------------------------------
# reconcile_session_log source-enum branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_session_when_session_id_missing():
    conn = _FakeConn(session_row=None)
    result = await reconcile_session_log(
        _FakePool(conn), session_id=uuid4(), log_import_id=uuid4()
    )
    assert result.source == "no_session"


@pytest.mark.asyncio
async def test_parse_failed_when_import_marked_failed():
    session = _make_session_row()
    import_row = {"id": uuid4(), "status": "failed", "error_message": "boom"}
    conn = _FakeConn(session_row=session, import_row=import_row)
    result = await reconcile_session_log(
        _FakePool(conn), session_id=session["session_id"], log_import_id=import_row["id"]
    )
    assert result.source == "parse_failed"
    assert result.notes.get("import_error") == "boom"


@pytest.mark.asyncio
async def test_parse_failed_when_import_row_missing():
    """A deleted import row maps to ``parse_failed`` rather than crashing."""
    session = _make_session_row()
    conn = _FakeConn(session_row=session, import_row=None)
    result = await reconcile_session_log(
        _FakePool(conn), session_id=session["session_id"], log_import_id=uuid4()
    )
    assert result.source == "parse_failed"


@pytest.mark.asyncio
async def test_no_log_entries_when_aggregate_count_is_zero():
    session = _make_session_row()
    import_row = {"id": uuid4(), "status": "parsed", "error_message": None}
    charger = _make_charger_aggregate(sample_count=0, max_energy_kwh=None, min_energy_kwh=None)
    conn = _FakeConn(session_row=session, import_row=import_row, charger_row=charger)
    result = await reconcile_session_log(
        _FakePool(conn), session_id=session["session_id"], log_import_id=import_row["id"]
    )
    assert result.source == "no_log_entries"


@pytest.mark.asyncio
async def test_reconciled_when_both_sides_present():
    session = _make_session_row()
    import_row = {"id": uuid4(), "status": "parsed", "error_message": None}
    charger = _make_charger_aggregate()  # 5.55 kWh, 4 samples
    energy_row = {"energy_kwh": 5.50}  # our telemetry: 5.50 kWh
    conn = _FakeConn(
        session_row=session,
        import_row=import_row,
        charger_row=charger,
        energy_row=energy_row,
    )
    result = await reconcile_session_log(
        _FakePool(conn), session_id=session["session_id"], log_import_id=import_row["id"]
    )
    assert result.source == "reconciled"
    assert result.our_energy_kwh == pytest.approx(5.50)
    assert result.charger_energy_kwh == pytest.approx(5.55)
    # delta = (5.55 - 5.50) / 5.50 ≈ +0.0091 (charger reported 0.9% more)
    assert result.energy_delta_pct == pytest.approx((5.55 - 5.50) / 5.50, rel=1e-3)
    assert result.our_duration_s == 3600
    assert result.charger_duration_s == 3600
    assert result.notes["charger_entries"] == 4


@pytest.mark.asyncio
async def test_partial_when_our_energy_is_missing():
    """No telemetry → ``partial`` (charger side has data, ours doesn't)."""
    session = _make_session_row()
    import_row = {"id": uuid4(), "status": "parsed", "error_message": None}
    charger = _make_charger_aggregate()
    energy_row = {"energy_kwh": 0}  # treated as missing
    conn = _FakeConn(
        session_row=session,
        import_row=import_row,
        charger_row=charger,
        energy_row=energy_row,
    )
    result = await reconcile_session_log(
        _FakePool(conn), session_id=session["session_id"], log_import_id=import_row["id"]
    )
    assert result.source == "partial"
    assert result.our_energy_kwh is None
    assert result.charger_energy_kwh == pytest.approx(5.55)
    assert result.energy_delta_pct is None


# ---------------------------------------------------------------------------
# write_session_log_reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_upserts_with_notes_as_json():
    conn = _FakeConn()
    pool = _FakePool(conn)
    session_id = uuid4()
    import_id = uuid4()
    result = ReconciliationResult(
        source="reconciled",
        our_energy_kwh=5.50,
        charger_energy_kwh=5.55,
        energy_delta_pct=0.009,
        our_duration_s=3600,
        charger_duration_s=3600,
        our_start_time=_NOW,
        charger_start_time=_NOW,
        our_end_time=_LATER,
        charger_end_time=_LATER,
        notes={"charger_entries": 4},
    )
    ok = await write_session_log_reconciliation(
        pool, session_id=session_id, log_import_id=import_id, result=result
    )
    assert ok is True
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "ON CONFLICT (session_id, log_import_id)" in sql
    assert args[0] == session_id
    assert args[1] == import_id
    assert args[11] == "reconciled"
    # notes is JSON-encoded
    import json

    assert json.loads(args[12]) == {"charger_entries": 4}
