"""Integration tests for the connector_status trigger (migration 029+).

After migration 029, fn_alerts_on_connector_status resolves tenant context
directly from connector_status.organization_id / depot_id — no shadow-table
JOIN. Tests insert connector_status rows with those columns set, verifying
the SQL, generated columns, partial unique index, ON CONFLICT inference, and
pg_notify all work end-to-end. Skipped when TEST_DATABASE_URL is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio


pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest_asyncio.fixture
async def db_pool():
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )
    try:
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4, command_timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"test database unavailable: {exc}")
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def org_depot_ids(db_pool):
    """Yield (org_id, depot_id, ocpp_id) as plain UUIDs; no shadow-table rows needed.

    The trigger resolves tenant context directly from connector_status columns
    (migration 029), so we only need to clean up notification_alerts and
    connector_status rows on teardown.
    """
    org_id = uuid4()
    depot_id = uuid4()
    ocpp_id = f"test_cp_{uuid4().hex[:8]}"

    yield org_id, depot_id, ocpp_id

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM notification_alerts WHERE organization_id = $1", org_id
        )
        await conn.execute(
            "DELETE FROM connector_status WHERE station_id = $1", ocpp_id
        )


async def _insert_status(
    conn: asyncpg.Connection,
    station_id: str,
    connector_id: int,
    status: str,
    *,
    organization_id: UUID | None = None,
    depot_id: UUID | None = None,
    error_code: str | None = None,
) -> None:
    """Insert a connector_status row, optionally setting tenant context columns."""
    await conn.execute(
        """
        INSERT INTO connector_status
            (station_id, connector_id, status, error_code, organization_id, depot_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        station_id,
        connector_id,
        status,
        error_code,
        organization_id,
        depot_id,
    )


async def _fetch_alert(
    conn: asyncpg.Connection, dedup_key: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM notification_alerts WHERE dedup_key = $1", dedup_key
    )


class TestConnectorStatusTrigger:
    """fn_alerts_on_connector_status behavior."""

    @pytest.mark.asyncio
    async def test_faulted_creates_critical_alert(self, db_pool, org_depot_ids):
        org_id, depot_id, ocpp_id = org_depot_ids
        async with db_pool.acquire() as conn:
            await _insert_status(
                conn, ocpp_id, 1, "Faulted",
                organization_id=org_id, depot_id=depot_id,
                error_code="PowerMeterFailure",
            )

            alert = await _fetch_alert(conn, f"charger_fault:{ocpp_id}:1")
            assert alert is not None
            assert alert["organization_id"] == org_id
            assert alert["depot_id"] == depot_id
            assert alert["alert_type"] == "charger_fault"
            assert alert["severity"] == "critical"
            assert alert["severity_level"] == 3
            assert alert["status"] == "active"
            assert alert["last_notified_at"] is None
            detail = json.loads(alert["detail"])
            assert detail["station_id"] == ocpp_id
            assert detail["connector_id"] == 1
            assert detail["status"] == "Faulted"
            assert detail["error_code"] == "PowerMeterFailure"

    @pytest.mark.asyncio
    async def test_unavailable_creates_warning_alert(self, db_pool, org_depot_ids):
        org_id, _, ocpp_id = org_depot_ids
        async with db_pool.acquire() as conn:
            await _insert_status(
                conn, ocpp_id, 2, "Unavailable",
                organization_id=org_id,
            )

            alert = await _fetch_alert(conn, f"charger_fault:{ocpp_id}:2")
            assert alert is not None
            assert alert["severity"] == "warning"
            assert alert["severity_level"] == 2

    @pytest.mark.asyncio
    async def test_repeat_fault_dedupes_and_bumps_last_occurrence(
        self, db_pool, org_depot_ids
    ):
        org_id, _, ocpp_id = org_depot_ids
        async with db_pool.acquire() as conn:
            await _insert_status(
                conn, ocpp_id, 1, "Faulted",
                organization_id=org_id, error_code="OverCurrentFailure",
            )
            first = await _fetch_alert(conn, f"charger_fault:{ocpp_id}:1")
            assert first is not None

            await asyncio.sleep(0.05)

            await _insert_status(
                conn, ocpp_id, 1, "Faulted",
                organization_id=org_id, error_code="OtherError",
            )
            after = await _fetch_alert(conn, f"charger_fault:{ocpp_id}:1")

            row_count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_alerts WHERE dedup_key = $1",
                f"charger_fault:{ocpp_id}:1",
            )
            assert row_count == 1, "second Faulted should UPSERT, not INSERT"
            assert after["id"] == first["id"]
            assert after["last_occurrence_at"] >= first["last_occurrence_at"]
            assert after["first_occurrence_at"] == first["first_occurrence_at"]
            assert json.loads(after["detail"])["error_code"] == "OtherError"

    @pytest.mark.asyncio
    async def test_recovery_resolves_active_alert(self, db_pool, org_depot_ids):
        org_id, _, ocpp_id = org_depot_ids
        async with db_pool.acquire() as conn:
            await _insert_status(
                conn, ocpp_id, 1, "Faulted",
                organization_id=org_id, error_code="PowerMeterFailure",
            )
            assert (await _fetch_alert(conn, f"charger_fault:{ocpp_id}:1"))["status"] == "active"

            # Recovery row does not need org context — trigger uses dedup_key to resolve.
            await _insert_status(conn, ocpp_id, 1, "Available")

            resolved = await _fetch_alert(conn, f"charger_fault:{ocpp_id}:1")
            assert resolved["status"] == "resolved"
            assert resolved["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_resolved_alert_can_reopen_with_new_row(
        self, db_pool, org_depot_ids
    ):
        org_id, _, ocpp_id = org_depot_ids
        async with db_pool.acquire() as conn:
            await _insert_status(conn, ocpp_id, 1, "Faulted", organization_id=org_id)
            await _insert_status(conn, ocpp_id, 1, "Available")
            await _insert_status(
                conn, ocpp_id, 1, "Faulted",
                organization_id=org_id, error_code="Re-occurred",
            )

            rows = await conn.fetch(
                "SELECT id, status FROM notification_alerts WHERE dedup_key = $1 ORDER BY created_at",
                f"charger_fault:{ocpp_id}:1",
            )
            assert len(rows) == 2, "resolved alert lets a new active row in"
            assert rows[0]["status"] == "resolved"
            assert rows[1]["status"] == "active"
            assert rows[0]["id"] != rows[1]["id"]

    @pytest.mark.asyncio
    async def test_null_org_silently_skipped(self, db_pool):
        """Faulted row without organization_id must not raise or create an alert."""
        station = f"noorg_{uuid4().hex[:8]}"
        async with db_pool.acquire() as conn:
            await _insert_status(conn, station, 1, "Faulted", error_code="Test")
            alert = await _fetch_alert(conn, f"charger_fault:{station}:1")
            assert alert is None
            await conn.execute(
                "DELETE FROM connector_status WHERE station_id = $1", station
            )

    @pytest.mark.asyncio
    async def test_pg_notify_fires_on_new_alert(self, db_pool, org_depot_ids):
        org_id, depot_id, ocpp_id = org_depot_ids
        payloads: list[str] = []
        received = asyncio.Event()

        def on_notify(_conn, _pid, _channel, payload):
            payloads.append(payload)
            received.set()

        listener = await db_pool.acquire()
        try:
            await listener.add_listener("notification_alerts_new", on_notify)

            async with db_pool.acquire() as writer:
                await _insert_status(
                    writer, ocpp_id, 1, "Faulted",
                    organization_id=org_id, depot_id=depot_id,
                    error_code="PowerMeterFailure",
                )

            try:
                await asyncio.wait_for(received.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pytest.fail("pg_notify did not fire within 2s")

            assert payloads, "expected at least one NOTIFY payload"
            payload = json.loads(payloads[0])
            assert payload["alert_type"] == "charger_fault"
            assert payload["severity"] == "critical"
            assert payload["depot_id"] == str(depot_id)
            assert payload["dedup_key"] == f"charger_fault:{ocpp_id}:1"
            assert UUID(payload["alert_id"])
        finally:
            await listener.remove_listener("notification_alerts_new", on_notify)
            await db_pool.release(listener)

    @pytest.mark.asyncio
    async def test_recovery_for_nonexistent_alert_is_noop(
        self, db_pool, org_depot_ids
    ):
        """An Available row when no active alert exists must not error or insert."""
        _, _, ocpp_id = org_depot_ids
        async with db_pool.acquire() as conn:
            await _insert_status(conn, ocpp_id, 1, "Available")
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_alerts WHERE dedup_key = $1",
                f"charger_fault:{ocpp_id}:1",
            )
            assert count == 0
