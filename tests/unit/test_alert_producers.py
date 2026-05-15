"""Unit tests for the four operational alert producers.

Producers covered:
- ``charger_auth_failure``      (websocket_handler.security_manager)
- ``degraded_optimization``     (core.controller)
- ``missing_input``             (core.controller)
- ``stale_telemetry``           (core.controller)

Each test runs the producer code path against an in-memory mock of the
``notifications.alerts.upsert_alert`` / ``resolve_alert`` repository so we
verify the wire-level contract (alert_type literal, severity, dedup_key
shape, body envelope) without spinning up Postgres.

These tests are the regression net for the alert-type strings the
frontend's KNOWN_ALERT_TYPES expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest


# ---------------------------------------------------------------------------
# Charger auth failure
# ---------------------------------------------------------------------------


class TestChargerAuthFailureProducer:
    """src/websocket_handler/security_manager.py: _emit_charger_auth_failure_alert."""

    @pytest.mark.asyncio
    async def test_emit_alert_uses_charger_auth_failure_type(self):
        from src.websocket_handler.security_manager import (
            SecurityConfig,
            SecurityManager,
        )

        timescale_client = MagicMock()
        # async context-manager pool
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = pool

        org_id = uuid4()
        depot_id = uuid4()
        charger_id = uuid4()
        conn.fetchrow = AsyncMock(
            return_value={
                "organization_id": org_id,
                "depot_id": depot_id,
                "depot_name": "Berlin Depot",
                "charger_id": charger_id,
                "charger_name": "Bay 1",
            }
        )

        manager = SecurityManager(timescale_client, SecurityConfig())

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert:
            await manager._emit_charger_auth_failure_alert("acme-001", "invalid_credentials")

        upsert.assert_awaited_once()
        kwargs = upsert.await_args.kwargs
        assert kwargs["alert_type"] == "charger_auth_failure"
        assert kwargs["severity"].value == "critical"
        assert kwargs["dedup_key"] == "charger_auth_failure:acme-001"
        assert kwargs["organization_id"] == org_id
        assert kwargs["depot_id"] == depot_id
        body = kwargs["detail"]
        assert "description" in body
        assert "suggestedAction" in body
        assert body["context"]["kind"] == "charger"
        assert body["station_id"] == "acme-001"
        assert body["reason"] == "invalid_credentials"

    @pytest.mark.asyncio
    async def test_unknown_station_no_emit(self):
        """Unknown stations skip alert emission (no org to attribute it to)."""
        from src.websocket_handler.security_manager import (
            SecurityConfig,
            SecurityManager,
        )

        timescale_client = MagicMock()
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = pool
        conn.fetchrow = AsyncMock(return_value=None)

        manager = SecurityManager(timescale_client, SecurityConfig())

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert:
            await manager._emit_charger_auth_failure_alert("ghost-station", "x")

        upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_clears_alert_on_success(self):
        from src.websocket_handler.security_manager import (
            SecurityConfig,
            SecurityManager,
        )

        timescale_client = MagicMock()
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        timescale_client.pg_pool = pool
        org_id = uuid4()
        conn.fetchval = AsyncMock(return_value=org_id)

        manager = SecurityManager(timescale_client, SecurityConfig())

        with patch(
            "src.notifications.alerts.resolve_alert", new_callable=AsyncMock
        ) as resolve:
            await manager._resolve_charger_auth_failure_alert("acme-001")

        resolve.assert_awaited_once()
        assert resolve.await_args.kwargs["dedup_key"] == "charger_auth_failure:acme-001"
        assert resolve.await_args.kwargs["organization_id"] == org_id


# ---------------------------------------------------------------------------
# Readiness-driven producers (controller)
# ---------------------------------------------------------------------------


@dataclass
class _StubReadiness:
    is_blocking: bool
    missing_inputs: list[str]
    degraded_reasons: list[str]
    assumptions: dict[str, Any]


@dataclass
class _StubSnapshot:
    readiness: _StubReadiness


class _StubAssembler:
    def __init__(self, organization_id: UUID):
        self.last_organization_id = str(organization_id)


class _StubController:
    """Minimal stand-in for DepotController exposing what the helper needs."""

    def __init__(
        self,
        depot_id: str,
        organization_id: UUID,
        pool,
        static_pool=None,
    ):
        self.depot_id = depot_id
        self.assembler = _StubAssembler(organization_id)
        pools = MagicMock()
        pools.ts = pool
        # Liveness guard reads from the static (Supabase) pool. Default to a
        # pool that reports the depot as live so existing tests keep passing.
        pools.static = static_pool or _make_live_static_pool()
        self.pools = pools

    # Attach the real methods under test.
    from src.core.controller import DepotController  # noqa: PLC0415

    _emit_readiness_alerts = DepotController._emit_readiness_alerts
    _depot_exists_in_supabase = DepotController._depot_exists_in_supabase


def _make_pool():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


def _make_live_static_pool():
    """Return a static-pool mock whose ``sites`` lookup returns one row."""
    pool, conn = _make_pool()
    conn.fetchrow = AsyncMock(return_value={"?column?": 1})
    return pool


def _make_dead_static_pool():
    """Return a static-pool mock whose ``sites`` lookup returns no row."""
    pool, conn = _make_pool()
    conn.fetchrow = AsyncMock(return_value=None)
    return pool


class TestMissingInputProducer:
    @pytest.mark.asyncio
    async def test_blocking_readiness_emits_missing_input(self):
        org_id = uuid4()
        depot_id = str(uuid4())
        pool, _ = _make_pool()
        ctrl = _StubController(depot_id, org_id, pool)
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=True,
                missing_inputs=["building_load", "schedules"],
                degraded_reasons=[],
                assumptions={},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert, patch(
            "src.notifications.alerts.resolve_alert", new_callable=AsyncMock
        ) as resolve:
            await ctrl._emit_readiness_alerts(snapshot, run_status=None)

        upsert.assert_awaited_once()
        kwargs = upsert.await_args.kwargs
        assert kwargs["alert_type"] == "missing_input"
        assert kwargs["severity"].value == "critical"
        assert kwargs["dedup_key"] == f"missing_input:{depot_id}"
        body = kwargs["detail"]
        assert body["missing_inputs"] == ["building_load", "schedules"]
        assert body["context"]["kind"] == "site"
        # Resolve isn't called when we're emitting missing_input.
        resolve.assert_not_awaited()


class TestDegradedOptimizationProducer:
    @pytest.mark.asyncio
    async def test_degraded_run_emits_degraded_optimization_and_resolves_missing(self):
        org_id = uuid4()
        depot_id = str(uuid4())
        pool, _ = _make_pool()
        ctrl = _StubController(depot_id, org_id, pool)
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=False,
                missing_inputs=[],
                degraded_reasons=["building_load_meter_unavailable"],
                assumptions={"building_load": {"source": "forecast_fallback"}},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert, patch(
            "src.notifications.alerts.resolve_alert", new_callable=AsyncMock
        ) as resolve:
            await ctrl._emit_readiness_alerts(snapshot, run_status="degraded")

        # Expect one upsert for degraded_optimization, plus a resolve for missing_input.
        # The stale_telemetry path also resolves because telemetry_all_defaulted
        # isn't in degraded_reasons.
        types = [c.kwargs["alert_type"] for c in upsert.await_args_list]
        assert "degraded_optimization" in types
        assert "missing_input" not in types

        resolved_keys = [c.kwargs["dedup_key"] for c in resolve.await_args_list]
        assert f"missing_input:{depot_id}" in resolved_keys
        assert f"stale_telemetry:{depot_id}" in resolved_keys

        degraded_call = next(
            c for c in upsert.await_args_list if c.kwargs["alert_type"] == "degraded_optimization"
        )
        assert degraded_call.kwargs["severity"].value == "warning"
        assert degraded_call.kwargs["dedup_key"] == f"degraded_optimization:{depot_id}"
        body = degraded_call.kwargs["detail"]
        assert body["degraded_reasons"] == ["building_load_meter_unavailable"]
        assert body["assumptions"] == {"building_load": {"source": "forecast_fallback"}}

    @pytest.mark.asyncio
    async def test_optimal_run_resolves_all_readiness_alerts(self):
        org_id = uuid4()
        depot_id = str(uuid4())
        pool, _ = _make_pool()
        ctrl = _StubController(depot_id, org_id, pool)
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=False,
                missing_inputs=[],
                degraded_reasons=[],
                assumptions={},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert, patch(
            "src.notifications.alerts.resolve_alert", new_callable=AsyncMock
        ) as resolve:
            await ctrl._emit_readiness_alerts(snapshot, run_status="optimal")

        upsert.assert_not_awaited()
        resolved = sorted(c.kwargs["dedup_key"] for c in resolve.await_args_list)
        assert resolved == [
            f"degraded_optimization:{depot_id}",
            f"missing_input:{depot_id}",
            f"stale_telemetry:{depot_id}",
        ]


class TestStaleTelemetryProducer:
    @pytest.mark.asyncio
    async def test_telemetry_all_defaulted_emits_stale_telemetry(self):
        org_id = uuid4()
        depot_id = str(uuid4())
        pool, _ = _make_pool()
        ctrl = _StubController(depot_id, org_id, pool)
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=False,
                missing_inputs=[],
                degraded_reasons=["telemetry_all_defaulted"],
                assumptions={"telemetry": {"source": "default_soc", "default_value": 0.5}},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert, patch(
            "src.notifications.alerts.resolve_alert", new_callable=AsyncMock
        ):
            await ctrl._emit_readiness_alerts(snapshot, run_status="degraded")

        types = [c.kwargs["alert_type"] for c in upsert.await_args_list]
        assert "stale_telemetry" in types
        stale_call = next(
            c for c in upsert.await_args_list if c.kwargs["alert_type"] == "stale_telemetry"
        )
        assert stale_call.kwargs["severity"].value == "warning"
        assert stale_call.kwargs["dedup_key"] == f"stale_telemetry:{depot_id}"
        assert stale_call.kwargs["detail"]["context"]["kind"] == "site"

    @pytest.mark.asyncio
    async def test_no_org_id_skips_emission(self):
        """When the assembler hasn't resolved the depot's organization, skip."""
        from src.core.controller import DepotController

        ctrl = MagicMock()
        ctrl.depot_id = str(uuid4())
        ctrl.assembler = _StubAssembler(uuid4())
        ctrl.assembler.last_organization_id = None
        pools = MagicMock()
        ctrl.pools = pools
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=True,
                missing_inputs=["building_load"],
                degraded_reasons=[],
                assumptions={},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert:
            await DepotController._emit_readiness_alerts(ctrl, snapshot, run_status=None)

        upsert.assert_not_awaited()


class TestStaleDepotSupabaseGuard:
    """Liveness guard for the depot's Supabase ``sites`` row.

    Prevents the controller from emitting alerts (and the dispatcher from
    logging "no recipients for org=…") after the org has been deleted
    upstream in Supabase but the local TimescaleDB org mirror still exists.
    """

    @pytest.mark.asyncio
    async def test_missing_sites_row_skips_alert_emission(self):
        org_id = uuid4()
        depot_id = str(uuid4())
        pool, _ = _make_pool()
        ctrl = _StubController(
            depot_id, org_id, pool, static_pool=_make_dead_static_pool()
        )
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=True,
                missing_inputs=["building_load"],
                degraded_reasons=[],
                assumptions={},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert, patch(
            "src.notifications.alerts.resolve_alert", new_callable=AsyncMock
        ) as resolve:
            await ctrl._emit_readiness_alerts(snapshot, run_status=None)

        upsert.assert_not_awaited()
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_supabase_error_fails_open_and_still_emits(self):
        """Transient Supabase errors should NOT silence the alert pipeline."""
        org_id = uuid4()
        depot_id = str(uuid4())
        pool, _ = _make_pool()

        # Static pool that raises on fetchrow — represents a Supabase blip.
        bad_static, bad_conn = _make_pool()
        bad_conn.fetchrow = AsyncMock(side_effect=RuntimeError("connection reset"))

        ctrl = _StubController(depot_id, org_id, pool, static_pool=bad_static)
        snapshot = _StubSnapshot(
            readiness=_StubReadiness(
                is_blocking=True,
                missing_inputs=["building_load"],
                degraded_reasons=[],
                assumptions={},
            )
        )

        with patch(
            "src.notifications.alerts.upsert_alert", new_callable=AsyncMock
        ) as upsert:
            await ctrl._emit_readiness_alerts(snapshot, run_status=None)

        upsert.assert_awaited_once()
        assert upsert.await_args.kwargs["alert_type"] == "missing_input"

    @pytest.mark.asyncio
    async def test_depot_exists_in_supabase_returns_true_on_hit(self):
        pool, _ = _make_pool()
        ctrl = _StubController(str(uuid4()), uuid4(), pool)
        assert await ctrl._depot_exists_in_supabase() is True

    @pytest.mark.asyncio
    async def test_depot_exists_in_supabase_returns_false_on_miss(self):
        pool, _ = _make_pool()
        ctrl = _StubController(
            str(uuid4()), uuid4(), pool, static_pool=_make_dead_static_pool()
        )
        assert await ctrl._depot_exists_in_supabase() is False
