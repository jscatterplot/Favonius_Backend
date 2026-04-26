"""Integration tests for ``GET /admin/ocpp/{cp_id}/state``.

The endpoint stitches together:
  * The DB rollup (``connector_status``, open ``charging_sessions``,
    ``charging_command_queue`` counts + last command).
  * The in-memory ``FleetChargePoint`` snapshot
    (``vendor``, ``model``, ``last_boot_at``, ``last_heartbeat_at``,
    subprotocol, ``connected``).

We mock both data sources so the test is self-contained and runs without
PostgreSQL or a live OCPP server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.websocket_handler.api_server import APIServer


pytestmark = pytest.mark.asyncio


def _make_server(*, ts_state: dict, cp=None) -> APIServer:
    """Construct an APIServer with mocked dependencies."""
    auth = MagicMock()
    auth.authenticate_user = AsyncMock(
        return_value={"id": "u-1", "role": "owner", "organization_id": "org-1"}
    )
    auth.authorize_action = AsyncMock(return_value=True)

    timescale = MagicMock()
    timescale.fetch_admin_state = AsyncMock(return_value=ts_state)

    websocket_server = MagicMock()
    websocket_server.get_charge_point.return_value = cp

    api = APIServer(
        config=MagicMock(),
        supabase_client=MagicMock(),
        auth_manager=auth,
        timescale_client=timescale,
        websocket_server=websocket_server,
    )
    return api


async def test_admin_state_returns_full_dump_for_connected_cpid() -> None:
    """End-to-end: connected charger + DB rows → fully-populated JSON."""
    boot_time = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    heartbeat_time = datetime(2026, 4, 26, 12, 5, tzinfo=timezone.utc)
    start_time = datetime(2026, 4, 26, 11, 30, tzinfo=timezone.utc)

    ts_state = {
        "connectors": [
            {
                "connector_id": 1,
                "status": "Charging",
                "error_code": "NoError",
                "updated_at": heartbeat_time,
            }
        ],
        "active_transactions": [
            {
                "transaction_id": 42,
                "connector_id": 1,
                "id_token": "TAG_ABC",
                "start_time": start_time,
            }
        ],
        "queue_counts": {"sent": 3},
        "last_command": {
            "queue_id": 17,
            "status": "sent",
            "sent_at": heartbeat_time,
            "acked_at": None,
            "enqueued_at": heartbeat_time,
            "last_error": None,
        },
    }

    # In-memory FleetChargePoint shim. The admin endpoint reads ``_cp`` first
    # (OCPP16Session attribute) then falls back to the object itself.
    inner = SimpleNamespace(
        vendor="ABB",
        model="Terra AC W11",
        last_boot_at=boot_time,
        last_heartbeat_at=heartbeat_time,
        subprotocol="ocpp1.6",
        _connection=None,
    )
    cp = SimpleNamespace(_cp=inner)

    api = _make_server(ts_state=ts_state, cp=cp)

    server = TestServer(api.app)
    async with TestClient(server) as client:
        resp = await client.get(
            "/admin/ocpp/PREFLIGHT-S3/state",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status == 200
        body = await resp.json()

    assert body["charge_point_id"] == "PREFLIGHT-S3"
    assert body["connected"] is True
    assert body["vendor"] == "ABB"
    assert body["model"] == "Terra AC W11"
    assert body["subprotocol"] == "ocpp1.6"
    assert body["last_boot_at"] == boot_time.isoformat()
    assert body["last_heartbeat_at"] == heartbeat_time.isoformat()
    assert body["connectors"] == [
        {
            "id": 1,
            "status": "Charging",
            "error_code": "NoError",
            "updated_at": heartbeat_time.isoformat(),
        }
    ]
    assert body["active_transactions"] == [
        {
            "transaction_id": 42,
            "connector_id": 1,
            "id_tag": "TAG_ABC",
            "start_time": start_time.isoformat(),
        }
    ]
    queue = body["queue"]
    assert queue["sent"] == 3
    assert queue["pending"] == 0  # zero-fill missing statuses
    assert queue["last_command"]["queue_id"] == 17


async def test_admin_state_404_for_unknown_cpid() -> None:
    """No DB rows + no in-memory session → 404."""
    ts_state = {
        "connectors": [],
        "active_transactions": [],
        "queue_counts": {},
        "last_command": None,
    }

    api = _make_server(ts_state=ts_state, cp=None)
    server = TestServer(api.app)
    async with TestClient(server) as client:
        resp = await client.get(
            "/admin/ocpp/UNKNOWN-CP/state",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status == 404


async def test_admin_state_disconnected_but_has_db_history() -> None:
    """Connector history but no live session → 200 with connected=False."""
    ts_state = {
        "connectors": [
            {
                "connector_id": 1,
                "status": "Unavailable",
                "error_code": "ConnectionLost",
                "updated_at": datetime(2026, 4, 26, tzinfo=timezone.utc),
            }
        ],
        "active_transactions": [],
        "queue_counts": {"pending": 2},
        "last_command": None,
    }

    api = _make_server(ts_state=ts_state, cp=None)
    server = TestServer(api.app)
    async with TestClient(server) as client:
        resp = await client.get(
            "/admin/ocpp/CHARGER_001/state",
            headers={"Authorization": "Bearer token"},
        )
        body = await resp.json()

    assert resp.status == 200
    assert body["connected"] is False
    assert body["vendor"] is None
    assert body["queue"]["pending"] == 2


async def test_admin_state_requires_owner_role() -> None:
    """Non-owner roles get 403 even with a valid token."""
    api = _make_server(
        ts_state={
            "connectors": [],
            "active_transactions": [],
            "queue_counts": {},
            "last_command": None,
        }
    )
    api.auth_manager.authenticate_user = AsyncMock(
        return_value={"id": "u-1", "role": "viewer", "organization_id": "org-1"}
    )

    server = TestServer(api.app)
    async with TestClient(server) as client:
        resp = await client.get(
            "/admin/ocpp/CHARGER_001/state",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status == 403


async def test_admin_state_rejects_oversized_cpid() -> None:
    api = _make_server(
        ts_state={
            "connectors": [],
            "active_transactions": [],
            "queue_counts": {},
            "last_command": None,
        }
    )
    server = TestServer(api.app)
    async with TestClient(server) as client:
        resp = await client.get(
            "/admin/ocpp/" + ("X" * 257) + "/state",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status == 400
