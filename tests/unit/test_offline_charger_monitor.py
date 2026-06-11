"""Unit tests for the charger offline-duration monitor.

Covers:
- ``plan_sweep``            — pure escalation / resolution logic (no DB).
- ``fetch_offline_chargers`` — the connector_status query's wire contract.
- ``OfflineChargerMonitor.run_once`` — fetch → plan → apply glue.

The alert-type literals (``charger_offline_3h`` / ``charger_offline_24h``) are
new strings the frontend's KNOWN_ALERT_TYPES will want to recognize; these
tests pin them so they can't drift silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.notifications.severity import Severity
from src.websocket_handler.offline_monitor import (
    ALERT_TYPE_3H,
    ALERT_TYPE_24H,
    OFFLINE_ALERT_TYPES,
    OfflineCharger,
    OfflineChargerMonitor,
    fetch_offline_chargers,
    plan_sweep,
)

WARN_AFTER_S = 10_800  # 3 h
ESCALATE_AFTER_S = 86_400  # 24 h
_SINCE = datetime(2026, 6, 3, 9, 28, tzinfo=timezone.utc)


def _charger(
    station_id: str,
    *,
    hours_offline: float,
    organization_id: Optional[UUID],
    depot_id: Optional[UUID] = None,
) -> OfflineCharger:
    return OfflineCharger(
        station_id=station_id,
        offline_since=_SINCE,
        offline_seconds=hours_offline * 3600.0,
        organization_id=organization_id,
        depot_id=depot_id,
    )


@dataclass
class _FakeAlert:
    """Minimal stand-in for notifications.alerts.Alert (plan_sweep reads 4 fields)."""

    alert_type: str
    organization_id: UUID
    dedup_key: str
    detail: dict[str, Any]


def _active(alert_type: str, station_id: str, org: UUID, *, with_detail: bool = True) -> _FakeAlert:
    return _FakeAlert(
        alert_type=alert_type,
        organization_id=org,
        dedup_key=f"{alert_type}:{station_id}",
        detail={"station_id": station_id} if with_detail else {},
    )


def _plan(offline, active):
    return plan_sweep(
        offline, active, warn_after_s=WARN_AFTER_S, escalate_after_s=ESCALATE_AFTER_S
    )


# ---------------------------------------------------------------------------
# plan_sweep — raising / escalating
# ---------------------------------------------------------------------------


class TestPlanSweepRaise:
    def test_warning_tier_only_between_3h_and_24h(self):
        org, depot = uuid4(), uuid4()
        plan = _plan([_charger("ST1", hours_offline=4, organization_id=org, depot_id=depot)], [])

        assert len(plan.upserts) == 1
        up = plan.upserts[0]
        assert up.alert_type == ALERT_TYPE_3H
        assert up.severity is Severity.WARNING
        assert up.dedup_key == "charger_offline_3h:ST1"
        assert up.organization_id == org
        assert up.depot_id == depot
        assert up.detail["station_id"] == "ST1"
        assert up.detail["threshold_hours"] == 3
        assert "offline" in up.title.lower()
        assert not plan.resolves
        assert not plan.skipped_no_tenant

    def test_critical_escalation_carries_both_rows_past_24h(self):
        org = uuid4()
        plan = _plan([_charger("ST1", hours_offline=25, organization_id=org)], [])

        by_type = {u.alert_type: u for u in plan.upserts}
        assert set(by_type) == {ALERT_TYPE_3H, ALERT_TYPE_24H}
        assert by_type[ALERT_TYPE_24H].severity is Severity.CRITICAL
        assert by_type[ALERT_TYPE_24H].dedup_key == "charger_offline_24h:ST1"
        assert by_type[ALERT_TYPE_24H].detail["threshold_hours"] == 24
        assert by_type[ALERT_TYPE_3H].severity is Severity.WARNING

    def test_below_warn_threshold_emits_nothing(self):
        # plan_sweep must guard even if a sub-threshold row slips through.
        plan = _plan([_charger("ST1", hours_offline=1, organization_id=uuid4())], [])
        assert not plan.upserts
        assert not plan.resolves

    def test_charger_without_tenant_is_skipped(self):
        plan = _plan([_charger("ST1", hours_offline=5, organization_id=None)], [])
        assert plan.upserts == []
        assert plan.skipped_no_tenant == ["ST1"]

    def test_multiple_chargers_independent_tiers(self):
        org = uuid4()
        plan = _plan(
            [
                _charger("ST1", hours_offline=4, organization_id=org),
                _charger("ST2", hours_offline=25, organization_id=org),
            ],
            [],
        )
        triples = {(u.alert_type, u.dedup_key) for u in plan.upserts}
        assert triples == {
            (ALERT_TYPE_3H, "charger_offline_3h:ST1"),
            (ALERT_TYPE_3H, "charger_offline_3h:ST2"),
            (ALERT_TYPE_24H, "charger_offline_24h:ST2"),
        }


# ---------------------------------------------------------------------------
# plan_sweep — resolving
# ---------------------------------------------------------------------------


class TestPlanSweepResolve:
    def test_reconnect_resolves_both_tiers(self):
        org = uuid4()
        active = [_active(ALERT_TYPE_3H, "ST1", org), _active(ALERT_TYPE_24H, "ST1", org)]
        plan = _plan([], active)  # charger no longer offline at all

        assert not plan.upserts
        resolved = {(r.organization_id, r.dedup_key) for r in plan.resolves}
        assert resolved == {
            (org, "charger_offline_3h:ST1"),
            (org, "charger_offline_24h:ST1"),
        }

    def test_back_under_24h_resolves_only_critical_keeps_warning(self):
        org = uuid4()
        active = [_active(ALERT_TYPE_3H, "ST1", org), _active(ALERT_TYPE_24H, "ST1", org)]
        # Still offline 5h → in breach_3h, not breach_24h.
        plan = _plan([_charger("ST1", hours_offline=5, organization_id=org)], active)

        assert [r.dedup_key for r in plan.resolves] == ["charger_offline_24h:ST1"]
        # The 3h warning is re-upserted (still breaching) and NOT resolved.
        assert any(u.alert_type == ALERT_TYPE_3H for u in plan.upserts)

    def test_resolve_parses_station_from_dedup_when_detail_missing(self):
        org = uuid4()
        plan = _plan([], [_active(ALERT_TYPE_3H, "STX", org, with_detail=False)])
        assert [r.dedup_key for r in plan.resolves] == ["charger_offline_3h:STX"]

    def test_still_breaching_charger_is_not_resolved(self):
        org = uuid4()
        active = [_active(ALERT_TYPE_3H, "ST1", org)]
        plan = _plan([_charger("ST1", hours_offline=10, organization_id=org)], active)
        assert plan.resolves == []

    def test_station_changed_tenant_resolves_former_org(self):
        # ST1 now breaches under new_org, but the former org still holds an
        # active alert for the same station — it must resolve (breach sets are
        # keyed by (org, station), not station alone).
        old_org, new_org = uuid4(), uuid4()
        plan = _plan(
            [_charger("ST1", hours_offline=5, organization_id=new_org)],
            [_active(ALERT_TYPE_3H, "ST1", old_org)],
        )
        assert [(r.organization_id, r.dedup_key) for r in plan.resolves] == [
            (old_org, "charger_offline_3h:ST1")
        ]
        assert any(
            u.alert_type == ALERT_TYPE_3H and u.organization_id == new_org
            for u in plan.upserts
        )


# ---------------------------------------------------------------------------
# fetch_offline_chargers — SQL contract
# ---------------------------------------------------------------------------


class TestFetchOfflineChargers:
    @pytest.mark.asyncio
    async def test_maps_rows_and_passes_threshold(self):
        org, depot = uuid4(), uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "station_id": "ST1",
                    "offline_since": _SINCE,
                    "offline_seconds": 13_000.5,
                    "organization_id": org,
                    "depot_id": depot,
                }
            ]
        )

        result = await fetch_offline_chargers(conn, min_offline_seconds=WARN_AFTER_S)

        assert len(result) == 1
        r = result[0]
        assert isinstance(r, OfflineCharger)
        assert r.station_id == "ST1"
        assert r.offline_seconds == pytest.approx(13_000.5)
        assert r.organization_id == org
        assert r.depot_id == depot
        assert r.offline_since == _SINCE

        conn.fetch.assert_awaited_once()
        sql, threshold = conn.fetch.await_args.args
        assert "'Unavailable'" in sql
        assert "'ConnectionLost'" in sql
        assert "EXTRACT(EPOCH FROM (NOW() - l.timestamp))" in sql
        assert "offline_seconds" in sql
        # Ordered by the server ingestion clock, not the charger timestamp.
        assert "created_at DESC" in sql
        assert threshold == pytest.approx(float(WARN_AFTER_S))

    @pytest.mark.asyncio
    async def test_empty_when_no_offline_rows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        assert await fetch_offline_chargers(conn, min_offline_seconds=WARN_AFTER_S) == []


# ---------------------------------------------------------------------------
# OfflineChargerMonitor.run_once — fetch → plan → apply
# ---------------------------------------------------------------------------


def _monitor_with_fakes(monkeypatch, *, offline, active):
    import src.websocket_handler.offline_monitor as mod

    fetch = AsyncMock(return_value=offline)
    listing = AsyncMock(return_value=active)
    upsert = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr(mod, "fetch_offline_chargers", fetch)
    monkeypatch.setattr(mod, "list_active_by_types", listing)
    monkeypatch.setattr(mod, "upsert_alert", upsert)
    monkeypatch.setattr(mod, "resolve_alert", resolve)
    # Default TOCTOU re-check: every planned station is still offline (no
    # suppression). Individual tests override to simulate a mid-sweep reconnect.
    monkeypatch.setattr(
        mod, "_stations_still_offline", AsyncMock(side_effect=lambda _conn, ids: set(ids))
    )

    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None

    monitor = OfflineChargerMonitor(
        pool=pool,
        warn_after_s=WARN_AFTER_S,
        escalate_after_s=ESCALATE_AFTER_S,
    )
    return monitor, fetch, listing, upsert, resolve


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_raises_both_tiers_for_long_outage(self, monkeypatch):
        org, depot = uuid4(), uuid4()
        monitor, fetch, listing, upsert, resolve = _monitor_with_fakes(
            monkeypatch,
            offline=[_charger("ST1", hours_offline=25, organization_id=org, depot_id=depot)],
            active=[],
        )

        plan = await monitor.run_once()

        assert len(plan.upserts) == 2
        assert upsert.await_count == 2
        resolve.assert_not_awaited()
        # SQL floor is the smaller threshold so one query feeds both tiers.
        fetch.assert_awaited_once()
        assert fetch.await_args.kwargs["min_offline_seconds"] == pytest.approx(float(WARN_AFTER_S))
        listing.assert_awaited_once()
        assert listing.await_args.args[1] == OFFLINE_ALERT_TYPES
        # Critical escalation is wired through to upsert_alert.
        sev_by_type = {c.kwargs["alert_type"]: c.kwargs["severity"] for c in upsert.await_args_list}
        assert sev_by_type[ALERT_TYPE_24H] is Severity.CRITICAL
        assert sev_by_type[ALERT_TYPE_3H] is Severity.WARNING

    @pytest.mark.asyncio
    async def test_resolves_when_charger_recovered(self, monkeypatch):
        org = uuid4()
        monitor, _fetch, _listing, upsert, resolve = _monitor_with_fakes(
            monkeypatch,
            offline=[],
            active=[_active(ALERT_TYPE_3H, "ST1", org)],
        )

        await monitor.run_once()

        upsert.assert_not_awaited()
        resolve.assert_awaited_once()
        assert resolve.await_args.kwargs["dedup_key"] == "charger_offline_3h:ST1"
        assert resolve.await_args.kwargs["organization_id"] == org

    @pytest.mark.asyncio
    async def test_suppresses_upsert_when_charger_reconnects_before_apply(self, monkeypatch):
        import src.websocket_handler.offline_monitor as mod

        org = uuid4()
        monitor, _fetch, _listing, upsert, resolve = _monitor_with_fakes(
            monkeypatch,
            offline=[_charger("ST1", hours_offline=25, organization_id=org)],
            active=[],
        )
        # Reconnected between the planning fetch and the apply re-check.
        monkeypatch.setattr(mod, "_stations_still_offline", AsyncMock(return_value=set()))

        plan = await monitor.run_once()

        assert len(plan.upserts) == 2  # planned (3h + 24h)
        upsert.assert_not_awaited()  # but suppressed at apply time
        resolve.assert_not_awaited()
