"""Unit tests for the Terra AC meter_start backfill script's classifier.

We mock the asyncpg connection and exercise the three decision paths:
register-sample backfill, Phase 2 synthesis, and skip. The DB-side SQL
is exercised through the mock's call assertions; the actual database
interaction is covered by the script being a thin wrapper over
``compute_energy_kwh`` and ``synthesize_energy_kwh_from_meter_stop``
helpers that already have full unit coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scripts.backfill_terra_meter_start import (
    _classify_and_backfill,
    _resolve_cap_wh,
)
from src.websocket_handler.meter_value_utils import DEFAULT_SYNTHESIZED_DELTA_CAP_WH


def _row(**overrides):
    base = {
        "session_id": "sess-1",
        "station_id": "hrx-uab_hrx-vilnius-005",
        "transaction_id": 18,
        "start_time": datetime(2026, 5, 14, 9, 11, 42, tzinfo=timezone.utc),
        "end_time": datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc),
        "meter_start_wh": 0,
        "meter_stop_wh": 2982,
        "last_meter_wh": None,
        "stop_reason": "EVDisconnected",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_classify_register_path_backfills_from_earliest_sample(capsys):
    """When telemetry_samples has Energy.Active.Import.Register, use it as start."""
    conn = AsyncMock()
    # earliest register sample in the window is 100 Wh; last_meter_wh = 3082.
    conn.fetchval = AsyncMock(return_value=100)
    row = _row(last_meter_wh=3082)

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=False)

    assert decision == "register"
    # The UPDATE is NOT executed in dry-run, but the classifier still printed.
    out = capsys.readouterr().out
    assert "register" in out
    assert "energy_delivered_kwh -> 2.982" in out  # (3082 - 100) / 1000


@pytest.mark.asyncio
async def test_classify_synthesis_path_when_no_register_samples(capsys):
    """No register samples + meter_stop ≤ cap → synthesize."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # No Energy.Active.Import.Register
    row = _row()  # meter_stop_wh=2982, last_meter_wh=None, meter_start_wh=0

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=False)

    assert decision == "synthesized"
    out = capsys.readouterr().out
    assert "synthesized" in out
    assert "energy_delivered_kwh -> 2.982" in out


@pytest.mark.asyncio
async def test_classify_synthesis_above_cap_skips(capsys):
    """meter_stop above the cap is not synthesized — row stays NULL."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    row = _row(meter_stop_wh=51_000)  # > 50 kWh cap

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=False)

    assert decision == "skipped"
    out = capsys.readouterr().out
    assert "skipped" in out


@pytest.mark.asyncio
async def test_classify_skips_when_last_meter_present_but_no_register():
    """last_meter_wh non-NULL means register samples DID arrive — synthesis suppressed.

    If last_meter_wh is populated but the earliest-register query returns
    None (e.g. the samples were pruned from telemetry_samples after
    retention), we cannot reconstruct a real meter_start. Don't fall back
    to synthesis — that would double-count the energy already captured in
    last_meter_wh.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    row = _row(last_meter_wh=5000)

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=False)

    assert decision == "skipped"


@pytest.mark.asyncio
async def test_classify_register_path_uses_meter_stop_when_last_meter_null():
    """When the row only has meter_stop_wh, use it as the terminal value."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=500)  # earliest register
    row = _row(meter_stop_wh=5000, last_meter_wh=None)

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=False)

    assert decision == "register"


@pytest.mark.asyncio
async def test_classify_register_path_applies_update_when_apply_true():
    """--apply mode issues the UPDATE with the computed values."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=100)
    row = _row(last_meter_wh=3082)

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=True)

    assert decision == "register"
    update_call = conn.execute.await_args
    # ($1 session_id, $2 new_meter_start, $3 energy_kwh, $4 new_stop_reason)
    assert update_call.args[1] == "sess-1"
    assert update_call.args[2] == 100  # backfilled meter_start_wh
    assert update_call.args[3] == pytest.approx(2.982)
    assert update_call.args[4] is None  # register path doesn't touch stop_reason


@pytest.mark.asyncio
async def test_classify_synthesis_suffixes_stop_reason_on_apply():
    """--apply mode for synthesis stamps the audit suffix."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    row = _row()

    decision = await _classify_and_backfill(conn, row, cap_wh=50_000, apply=True)

    assert decision == "synthesized"
    update_call = conn.execute.await_args
    assert update_call.args[4] == "EVDisconnected|synthesized_delta"


def test_resolve_cap_wh_default():
    """No CLI, no env -> 50 kWh default."""
    assert _resolve_cap_wh(None) == DEFAULT_SYNTHESIZED_DELTA_CAP_WH


def test_resolve_cap_wh_cli_overrides(monkeypatch):
    """CLI argument wins over env var."""
    monkeypatch.setenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH", "10")
    assert _resolve_cap_wh(75.0) == 75_000


def test_resolve_cap_wh_env_when_no_cli(monkeypatch):
    """Env var used when no CLI override."""
    monkeypatch.setenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH", "30")
    assert _resolve_cap_wh(None) == 30_000


def test_resolve_cap_wh_bad_env_falls_back(monkeypatch):
    """Garbage env value falls back to the safe default."""
    monkeypatch.setenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH", "garbage")
    assert _resolve_cap_wh(None) == DEFAULT_SYNTHESIZED_DELTA_CAP_WH
