"""Wire-shape regression for GET /depots/{id}/alerts.

Pins the field names the frontend's AlertItemSchema requires:
``alert_id, organization_id, depot_id, depot_name, alert_type, severity,
subject, body, dedup_key, status, first_seen_at, last_seen_at,
occurrence_count, acknowledged_by, resolved_at, last_notified_at``.

If the API stops emitting any of these, this test fails first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import status as http_status

from src.api.main import app
from src.security.tenant_mirror import ensure_tenant_mirrored


AUTH_HDR = {"Authorization": "Bearer test"}


def _override_user(role: str = "favonius_admin"):
    user = {"sub": str(uuid4()), "app_metadata": {"favonius_role": role}}

    def override():
        return user

    app.dependency_overrides[ensure_tenant_mirrored] = override


def test_depot_alerts_endpoint_emits_frontend_aligned_wire_shape(client, mock_db_pool):
    """Directly mocks the alerts repository to isolate wire-shape assertions."""
    from src.notifications.alerts import Alert
    from src.notifications.severity import Severity

    pool, conn = mock_db_pool
    depot_id = str(uuid4())
    org_id = uuid4()
    alert_id = uuid4()
    alert_depot_id = uuid4()
    now = datetime.now(timezone.utc)
    _override_user()

    # depot row + optimization run row, in order
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"name": "Berlin Depot"},
            {
                "run_id": uuid4(),
                "run_time": now,
                "status": "optimal",
                "solver_used": "gurobi",
                "solve_time_s": 5.0,
            },
        ]
    )
    conn.fetch = AsyncMock(side_effect=[[], []])  # chargers, faults

    fake_alert = Alert(
        id=alert_id,
        organization_id=org_id,
        depot_id=alert_depot_id,
        alert_type="charger_auth_failure",
        severity=Severity.CRITICAL,
        title="Charger Bay 1 authentication failed",
        detail={
            "description": "Bay 1 failed to authenticate",
            "suggestedAction": "Rotate credentials",
            "context": {"kind": "charger", "id": str(uuid4())},
        },
        dedup_key="charger_auth_failure:acme-001",
        status="active",
        first_occurrence_at=now,
        last_occurrence_at=now,
        occurrence_count=7,
        acknowledged_at=None,
        acknowledged_by=None,
        resolved_at=None,
        last_notified_at=None,
        last_notified_count=0,
    )

    with patch("src.api.main.db_pools", pool), patch(
        "src.api.main.verify_depot_access", new_callable=AsyncMock
    ), patch(
        "src.notifications.alerts.list_for_depot",
        new_callable=AsyncMock,
        return_value=[fake_alert],
    ):
        response = client.get(f"/depots/{depot_id}/alerts", headers=AUTH_HDR)

    app.dependency_overrides.clear()
    assert response.status_code == http_status.HTTP_200_OK, response.text
    body = response.json()
    assert body["notification_alerts"], body
    alert = body["notification_alerts"][0]

    # Required field names (frontend AlertItemSchema)
    assert alert["alert_id"] == str(alert_id)
    assert alert["organization_id"] == str(org_id)
    assert alert["depot_id"]  # any UUID — the mock row has a different one
    assert alert["depot_name"] == "Berlin Depot"
    assert alert["alert_type"] == "charger_auth_failure"
    assert alert["severity"] == "critical"
    assert alert["subject"] == "Charger Bay 1 authentication failed"
    assert alert["dedup_key"] == "charger_auth_failure:acme-001"
    assert alert["status"] == "active"
    assert alert["occurrence_count"] == 7
    assert alert["first_seen_at"]
    assert alert["last_seen_at"]
    # Optional fields present (even if null)
    assert "acknowledged_by" in alert
    assert "resolved_at" in alert
    assert "last_notified_at" in alert
    # Body envelope
    body_payload = alert["body"]
    assert body_payload["description"]
    assert body_payload["suggestedAction"]
    assert body_payload["context"]["kind"] == "charger"

    # camelCase field names from the old shape are NOT present.
    for old_field in (
        "id",
        "title",
        "detail",
        "first_occurrence_at",
        "last_occurrence_at",
    ):
        assert old_field not in alert, f"old field '{old_field}' leaked into wire shape"
