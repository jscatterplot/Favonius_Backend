"""Cross-org admin endpoints: organizations, depots, charger credentials.

Covers RBAC for every (endpoint, role) combination and verifies:
- ``admin.read`` audit row written for each cross-org read by favonius_admin.
- ``charger.credentials.rotated`` audit row on rotation success.
- 403 (not 404) when a customer_admin reads another org's depots.
- Plaintext credentials are not retrievable after creation; only the rotation
  endpoint returns plaintext, and exactly once.

Reference: PRD Section 9.1, ``src/security/admin_audit.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi import status as http_status

from src.api.main import app
from src.security.rate_limiter import get_rate_limiter
from src.security.tenant_mirror import ensure_tenant_mirrored

AUTH_HDR = {"Authorization": "Bearer test-token"}
DEPOT_ID = str(uuid4())
OTHER_DEPOT_ID = str(uuid4())
CHARGER_ID = str(uuid4())
ORG_ID = str(uuid4())
OTHER_ORG_ID = str(uuid4())


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    """Reset FastAPI dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Reset the in-memory rate limiter so this module's calls don't leak into other tests."""
    get_rate_limiter().reset_in_memory_buckets_for_tests()
    yield
    get_rate_limiter().reset_in_memory_buckets_for_tests()


def _user(role: str, organization_id: str | None = None) -> dict:
    """Build a JWT-style payload using Supabase ``app_metadata``.

    favonius_admin never carries an organization_id; other roles default
    to ``ORG_ID`` unless explicitly overridden.
    """
    meta: dict = {"favonius_role": role}
    if role != "favonius_admin":
        meta["organization_id"] = organization_id or ORG_ID
    return {"sub": str(uuid4()), "app_metadata": meta}


def _override_user(user: dict):
    def _factory():
        return user

    app.dependency_overrides[ensure_tenant_mirrored] = _factory


def _depot_row(*, depot_id: str = DEPOT_ID, organization_id: str = ORG_ID) -> dict:
    return {
        "depot_id": depot_id,
        "organization_id": organization_id,
        "name": "Test Depot",
        "latitude": 0.0,
        "longitude": 0.0,
        "timezone": "UTC",
        "currency": "EUR",
        "utility_id": None,
        "max_grid_kw": 800.0,
        "demand_charge_rate_kw": 0.0,
        "demand_charge_billing_period": "monthly",
        "address": {},
        "billing_metadata": {},
        "building_load_source": {},
    }


def _charger_status_row(
    *,
    depot_id: str = DEPOT_ID,
    charger_id: str = CHARGER_ID,
    configured: bool = True,
) -> dict:
    """Mimic the row returned by ``get_charger_credentials_status``."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "charger_id": charger_id,
        "depot_id": depot_id,
        "ocpp_id": "acme-berlin-001",
        "credentials_created_at": now if configured else None,
        "credentials_last_rotated_at": None,
        "credentials_active": configured,
    }


# Static mock for asyncpg pool (acquire returns AsyncContextManager)
@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__.return_value = None
    conn.transaction.return_value.__aexit__.return_value = None
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool, conn


# ── GET /admin/organizations ────────────────────────────────────────────────


class TestListOrganizationsRBAC:
    """GET /admin/organizations — favonius_admin only."""

    def _hit(self, client):
        return client.get("/admin/organizations", headers=AUTH_HDR)

    def test_favonius_admin_200_with_audit(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("favonius_admin"))
        organizations = [{"organization_id": ORG_ID, "name": "Acme"}]
        conn.fetch = AsyncMock(return_value=organizations)
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["count"] == 1
        assert body["organizations"] == organizations
        # Cross-org admin read recorded
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "admin.read"
        assert row.actor_role == "favonius_admin"
        assert row.target_type == "organization"

    def test_customer_admin_403(self, client, mock_pool):
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_customer_operator_403(self, client, mock_pool):
        _override_user(_user("customer_operator"))
        response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_viewer_403(self, client, mock_pool):
        _override_user(_user("viewer"))
        response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"


# ── GET /admin/organizations/{org_id}/depots ────────────────────────────────


class TestListOrganizationDepotsRBAC:
    """GET /admin/organizations/{org_id}/depots — favonius_admin or matching customer_admin."""

    def _hit(self, client, org_id: str = ORG_ID):
        return client.get(f"/admin/organizations/{org_id}/depots", headers=AUTH_HDR)

    def test_favonius_admin_any_org_200_with_audit(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("favonius_admin"))
        depots = [_depot_row(organization_id=OTHER_ORG_ID)]
        conn.fetch = AsyncMock(return_value=depots)
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client, OTHER_ORG_ID)
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["count"] == 1
        assert response.json()["organization_id"] == OTHER_ORG_ID
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "admin.read"
        assert row.target_id == OTHER_ORG_ID

    def test_customer_admin_own_org_200_no_audit(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetch = AsyncMock(return_value=[_depot_row()])
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client, ORG_ID)
        assert response.status_code == http_status.HTTP_200_OK
        # NOT a cross-org read — own org reads do not generate admin.read.
        audit.assert_not_awaited()

    def test_customer_admin_other_org_403_not_404(self, client, mock_pool):
        """Critical: must return 403 (not 404) so existence does not leak."""
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with patch("src.api.main.db_pools", pool):
            response = self._hit(client, OTHER_ORG_ID)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.status_code != http_status.HTTP_404_NOT_FOUND
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ORGANIZATION"

    def test_customer_admin_missing_org_id_403(self, client):
        user = {"sub": str(uuid4()), "app_metadata": {"favonius_role": "customer_admin"}}
        _override_user(user)
        response = self._hit(client, ORG_ID)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "MISSING_ORGANIZATION"

    def test_customer_operator_403(self, client, mock_pool):
        _override_user(_user("customer_operator"))
        response = self._hit(client, ORG_ID)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_viewer_403(self, client, mock_pool):
        _override_user(_user("viewer"))
        response = self._hit(client, ORG_ID)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"


# ── GET /admin/depots/{depot_id}/chargers/{charger_id}/credentials_status ───


class TestCredentialsStatusRBAC:
    """GET .../credentials_status — favonius_admin or matching tenant."""

    URL = f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/credentials_status"

    def _hit(self, client):
        return client.get(self.URL, headers=AUTH_HDR)

    def test_favonius_admin_200_with_audit(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=_charger_status_row(),
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["configured"] is True
        assert body["depot_id"] == DEPOT_ID
        assert body["charger_id"] == CHARGER_ID
        # Plaintext password NEVER appears in the response
        assert "password" not in response.text.lower()
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "admin.read"
        assert row.target_type == "charger"

    def test_customer_admin_own_depot_200_no_audit(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=_charger_status_row(),
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        # Own-org read is NOT cross-org — no audit row written
        audit.assert_not_awaited()
        # Plaintext credentials never returned
        assert "password" not in response.text.lower()

    def test_customer_admin_other_org_depot_403_not_404(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_DEPOT"

    def test_customer_operator_own_depot_200_no_audit(self, client, mock_pool):
        """customer_operator can read credentials status for its own depot."""
        pool, _ = mock_pool
        _override_user(_user("customer_operator", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=_charger_status_row(),
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        audit.assert_not_awaited()

    def test_customer_operator_other_org_depot_403(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_operator", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_viewer_403(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("viewer"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(),
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_response_never_contains_password_hash(self, client, mock_pool):
        """The response body must never expose password_hash even by accident."""
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        # If our query helper accidentally added password_hash, the endpoint
        # must still strip it.
        bad_row = _charger_status_row()
        bad_row["password_hash"] = "$2b$12$some.hash.that.must.not.leak"
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=bad_row,
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body_text = response.text
        # Defensive: the response must not surface the hash, plaintext, or the
        # password key at all.
        assert "password_hash" not in body_text
        assert "$2b$" not in body_text
        assert "password" not in body_text.lower()


# ── POST /admin/depots/{depot_id}/chargers/{charger_id}/rotate_credentials ──


class TestRotateCredentialsRBAC:
    """POST .../rotate_credentials — favonius_admin or matching customer_admin."""

    URL = f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/rotate_credentials"

    def _hit(self, client):
        return client.post(self.URL, headers=AUTH_HDR)

    def test_favonius_admin_200_returns_plaintext_once(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        from datetime import datetime, timezone

        rotated_at = datetime.now(timezone.utc)
        rotated_row = {
            "ocpp_id": "acme-berlin-001",
            "last_rotated_at": rotated_at,
            "created_at": rotated_at,
        }
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.rotate_charger_credentials",
                new_callable=AsyncMock,
                return_value=rotated_row,
            ) as rotate_mock,
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["credentials"]["shown_once"] is True
        assert body["credentials"]["password"]
        # The plaintext returned must NOT equal the bcrypt hash that was stored.
        passed_hash = rotate_mock.await_args.kwargs["new_password_hash"]
        assert body["credentials"]["password"] != passed_hash
        assert passed_hash.startswith("$2")
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "charger.credentials.rotated"
        assert row.actor_role == "favonius_admin"
        # The audit metadata must NEVER include the plaintext password.
        assert body["credentials"]["password"] not in str(row.metadata)
        assert "password" not in row.metadata

    def test_customer_admin_own_org_200_returns_plaintext_once(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        from datetime import datetime, timezone

        rotated_at = datetime.now(timezone.utc)
        rotated_row = {
            "ocpp_id": "acme-berlin-001",
            "last_rotated_at": rotated_at,
            "created_at": rotated_at,
        }
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.rotate_charger_credentials",
                new_callable=AsyncMock,
                return_value=rotated_row,
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["credentials"]["password"]
        # Even non-cross-org rotations must be audited
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "charger.credentials.rotated"
        assert row.actor_role == "customer_admin"

    def test_customer_admin_other_org_403_not_404(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_customer_operator_403(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_operator", organization_id=ORG_ID))
        with patch("src.api.main.db_pools", pool):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_viewer_403(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("viewer"))
        with patch("src.api.main.db_pools", pool):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_charger_not_found_404(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.rotate_charger_credentials",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND
        # No audit row written for a 404 — only successful rotations are audited.
        audit.assert_not_awaited()


# ── POST /admin/depots/{depot_id}/chargers/{charger_id}/local_auth/reset ───


class TestResetLocalAuthCacheRBAC:
    """POST .../local_auth/reset — favonius_admin or matching customer_admin.

    Mirrors ``TestRotateCredentialsRBAC`` because the auth contract is the
    same: cross-org reads are 403 (not 404), customer_operator/viewer are
    403, the success path writes a ``charger.local_auth.cache_reset``
    audit row.
    """

    URL = f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/local_auth/reset"

    def _hit(self, client):
        return client.post(self.URL, headers=AUTH_HDR)

    def _reset_row(self, **overrides) -> dict:
        row = {
            "ocpp_id": "acme-berlin-001",
            "previous_supported": False,
            "previous_probed_firmware": "TAC3Z9119006710273::V1.8.36",
            "legacy_schema": False,
        }
        row.update(overrides)
        return row

    def test_favonius_admin_200_clears_cache_and_audits(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.reset_local_auth_cache",
                new_callable=AsyncMock,
                return_value=self._reset_row(),
            ) as reset_mock,
            patch(
                "src.api.main.write_admin_audit_row", new_callable=AsyncMock
            ) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["previous_supported"] is False
        assert body["previous_probed_firmware"] == "TAC3Z9119006710273::V1.8.36"
        assert body["ocpp_id"] == "acme-berlin-001"
        assert body["reset_at"]
        reset_mock.assert_awaited_once()
        # Audit row carries the previous cache state for ops forensics.
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "charger.local_auth.cache_reset"
        assert row.actor_role == "favonius_admin"
        assert row.metadata["previous_supported"] is False
        assert row.metadata["previous_probed_firmware"] == "TAC3Z9119006710273::V1.8.36"

    def test_customer_admin_own_org_200(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.reset_local_auth_cache",
                new_callable=AsyncMock,
                return_value=self._reset_row(),
            ),
            patch(
                "src.api.main.write_admin_audit_row", new_callable=AsyncMock
            ) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "charger.local_auth.cache_reset"
        assert row.actor_role == "customer_admin"

    def test_customer_admin_other_org_403_not_404(self, client, mock_pool):
        """A customer_admin querying a depot in a different org must get
        403, not 404 — same posture as rotate_credentials so we never leak
        depot existence across tenants."""
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_customer_operator_403(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_operator", organization_id=ORG_ID))
        with patch("src.api.main.db_pools", pool):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_viewer_403(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("viewer"))
        with patch("src.api.main.db_pools", pool):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_charger_not_found_404(self, client, mock_pool):
        """Unknown (depot, charger) pair → 404 and no audit row."""
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.reset_local_auth_cache",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.api.main.write_admin_audit_row", new_callable=AsyncMock
            ) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND
        audit.assert_not_awaited()

    def test_legacy_schema_still_returns_200(self, client, mock_pool):
        """When migration 012 is absent, the reset still succeeds (probe
        columns being NULL is the same as freshly cleared). The response
        records the legacy_schema fact for ops visibility."""
        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.reset_local_auth_cache",
                new_callable=AsyncMock,
                return_value=self._reset_row(
                    previous_supported=None,
                    previous_probed_firmware=None,
                    legacy_schema=True,
                ),
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["previous_supported"] is None
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.metadata["legacy_schema"] is True


# ── POST /admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize ───


class TestManualAuthorizeCooldown:
    """manual_authorize cooldown is per-connector for a full 60 seconds."""

    URL = f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/manual_authorize"

    def _hit(self, client):
        return client.post(
            self.URL,
            headers=AUTH_HDR,
            json={"connector_id": 1, "expires_in_seconds": 60},
        )

    def test_cooldown_blocks_even_when_recent_override_is_consumed(self, client, mock_pool):
        """A consumed override from the last 60s still returns 409.

        Regression guard: the cooldown query must not require ``consumed_at IS NULL``,
        otherwise fast charger consumption bypasses double-click protection.
        """
        from datetime import datetime, timedelta, timezone

        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))

        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"ocpp_id": "acme-berlin-001"},
                {
                    "id": uuid4(),
                    "created_at": now - timedelta(seconds=10),
                    "expires_at": now + timedelta(seconds=50),
                },
            ]
        )
        conn.execute = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock()

        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main._resolve_depot_for_admin",
                new_callable=AsyncMock,
                return_value=({"organization_id": ORG_ID}, None),
            ),
        ):
            response = self._hit(client)

        assert response.status_code == http_status.HTTP_409_CONFLICT
        body = response.json()["detail"]
        assert body["error_code"] == "RECENT_OVERRIDE_EXISTS"
        assert body["retry_after_seconds"] > 0

        cooldown_query = conn.fetchrow.await_args_list[1].args[0]
        assert "consumed_at IS NULL" not in cooldown_query
        conn.fetchval.assert_not_awaited()


# ── Plaintext non-retrievability ───────────────────────────────────────────


class TestPlaintextNotRetrievable:
    """The plaintext credential is only ever returned in two places: charger
    creation (existing endpoint) and credential rotation (this PR). No other
    endpoint may return a ``password`` field. The ``credentials_status``
    endpoint NEVER returns plaintext.
    """

    def test_status_endpoint_does_not_expose_plaintext_or_hash(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=_charger_status_row(),
            ),
        ):
            response = client.get(
                f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/credentials_status",
                headers=AUTH_HDR,
            )
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        # Whitelist of allowed fields
        allowed = {
            "depot_id",
            "charger_id",
            "ocpp_id",
            "configured",
            "created_at",
            "last_rotated_at",
        }
        assert (
            set(body.keys()) <= allowed
        ), f"Unexpected fields in credentials_status response: {set(body.keys()) - allowed}"
        assert "password" not in response.text.lower()
        assert "$2b$" not in response.text

    def test_rotate_returns_plaintext_exactly_once(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        from datetime import datetime, timezone

        rotated_at = datetime.now(timezone.utc)
        rotated_row = {
            "ocpp_id": "acme-berlin-001",
            "last_rotated_at": rotated_at,
            "created_at": rotated_at,
        }
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.rotate_charger_credentials",
                new_callable=AsyncMock,
                return_value=rotated_row,
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock),
        ):
            first = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/rotate_credentials",
                headers=AUTH_HDR,
            )
            assert first.status_code == http_status.HTTP_200_OK
            first_pwd = first.json()["credentials"]["password"]

            # A subsequent GET on the status endpoint must NOT return the
            # previously generated plaintext (it isn't stored anywhere
            # retrievable except the bcrypt hash).
            with patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=_charger_status_row(),
            ):
                follow = client.get(
                    f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/credentials_status",
                    headers=AUTH_HDR,
                )
            assert follow.status_code == http_status.HTTP_200_OK
            assert first_pwd not in follow.text
            assert "password" not in follow.text.lower()


# ── H3: audit-on-404 for admin depot lookups + strict mode ──────────────────


class TestAdminAudit404AndStrictMode:
    """``_resolve_depot_for_admin`` must record an ``admin.read`` row before
    returning 404 to a favonius_admin so negative-result enumeration is
    audited.  Cross-org enumeration paths use ``strict=True`` so a failed
    audit insert raises 503 instead of silently returning data."""

    URL = f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/credentials_status"

    def test_favonius_admin_404_writes_strict_audit_row(self, client, mock_pool):
        """Nonexistent depot for favonius_admin → 404 AND audit row written."""
        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = client.get(self.URL, headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND
        # Audit row was written before the 404 was raised
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "admin.read"
        assert row.target_type == "depot"
        assert row.metadata["result"] == "not_found"
        # Strict mode propagated to the writer
        assert audit.await_args.kwargs.get("strict") is True

    def test_strict_audit_failure_returns_503_not_404(self, client, mock_pool):
        """If the strict audit insert fails, the request fails closed (503)."""
        from src.security.admin_audit import AdminAuditWriteError

        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.api.main.write_admin_audit_row",
                new_callable=AsyncMock,
                side_effect=AdminAuditWriteError("simulated audit DB outage"),
            ),
        ):
            response = client.get(self.URL, headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE
        body = response.json()
        assert body["error_code"] == "AUDIT_LOG_UNAVAILABLE"
        # No depot data leaked in the failure response
        assert "depot_id" not in body

    def test_customer_admin_other_org_403_writes_no_audit(self, client, mock_pool):
        """customer_admin probing another org's depot must NOT generate an
        audit row — leak-resistant 403 means audit volume cannot be used as
        a side channel for existence."""
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = client.get(self.URL, headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        audit.assert_not_awaited()

    def test_favonius_admin_charger_not_found_writes_audit_row(self, client, mock_pool):
        """Depot exists but charger doesn't → 404 AND audit row written
        (cross-org admin enumerating chargers within a depot)."""
        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.get_charger_credentials_status",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock) as audit,
        ):
            response = client.get(self.URL, headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND
        # Cross-org charger lookup still records an admin.read row
        audit.assert_awaited_once()
        row = audit.await_args.args[1]
        assert row.action == "admin.read"
        assert row.target_type == "charger"
        assert row.metadata["result"] == "not_found"
        assert audit.await_args.kwargs.get("strict") is True

    def test_list_organizations_strict_audit_propagates_503(self, client, mock_pool):
        """The `/admin/organizations` listing uses strict audit; if the audit
        write fails the endpoint must return 503, not 200 + data."""
        from src.security.admin_audit import AdminAuditWriteError

        pool, _ = mock_pool
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.list_all_organizations",
                new_callable=AsyncMock,
                return_value=[
                    {"organization_id": ORG_ID, "name": "Acme"},
                ],
            ),
            patch(
                "src.api.main.write_admin_audit_row",
                new_callable=AsyncMock,
                side_effect=AdminAuditWriteError("audit unavailable"),
            ),
        ):
            response = client.get("/admin/organizations", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE
        body = response.json()
        assert body["error_code"] == "AUDIT_LOG_UNAVAILABLE"


# ── H3: write_admin_audit_row strict-mode unit semantics ────────────────────


class TestWriteAdminAuditRowStrictMode:
    """Unit-level coverage for the writer's strict/best-effort modes."""

    def test_strict_propagates_db_error(self):
        import asyncio

        from src.security.admin_audit import (
            AdminAuditRow,
            AdminAuditWriteError,
            write_admin_audit_row,
        )

        bad_pool = MagicMock()
        bad_acquire = MagicMock()
        bad_acquire.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
        bad_acquire.__aexit__ = AsyncMock(return_value=None)
        bad_pool.acquire.return_value = bad_acquire

        row = AdminAuditRow(action="admin.read", target_type="depot")

        async def _run():
            with pytest.raises(AdminAuditWriteError):
                await write_admin_audit_row(bad_pool, row, strict=True)

        asyncio.run(_run())

    def test_best_effort_swallows_db_error(self):
        import asyncio

        from src.security.admin_audit import AdminAuditRow, write_admin_audit_row

        bad_pool = MagicMock()
        bad_acquire = MagicMock()
        bad_acquire.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
        bad_acquire.__aexit__ = AsyncMock(return_value=None)
        bad_pool.acquire.return_value = bad_acquire

        row = AdminAuditRow(action="admin.read", target_type="depot")

        async def _run():
            # Default strict=False — must not raise
            await write_admin_audit_row(bad_pool, row)

        asyncio.run(_run())

    def test_strict_with_no_pool_raises(self):
        import asyncio

        from src.security.admin_audit import (
            AdminAuditRow,
            AdminAuditWriteError,
            write_admin_audit_row,
        )

        async def _run():
            with pytest.raises(AdminAuditWriteError):
                await write_admin_audit_row(None, AdminAuditRow(action="admin.read"), strict=True)

        asyncio.run(_run())

    def test_best_effort_with_no_pool_does_not_raise(self):
        import asyncio

        from src.security.admin_audit import AdminAuditRow, write_admin_audit_row

        async def _run():
            await write_admin_audit_row(None, AdminAuditRow(action="admin.read"))

        asyncio.run(_run())


# ── GET /admin/depots/{depot_id}/chargers/{charger_id}/sessions ─────────────


class TestListChargerCompletedSessionsRBAC:
    """GET .../chargers/{id}/sessions — favonius_admin or matching customer_admin.

    Feeds the charger-logs admin UI: operator picks a session, then triggers
    ``POST .../sessions/{session_id}/fetch_logs``. Role gate matches the
    action endpoint so the listing and the workflow it leads into share
    the same audience.
    """

    URL = f"/admin/depots/{DEPOT_ID}/chargers/{CHARGER_ID}/sessions"

    def _hit(self, client, query: str = ""):
        return client.get(f"{self.URL}{query}", headers=AUTH_HDR)

    @staticmethod
    def _session_row(*, source: str = "live", ocpp_id: str = "acme-berlin-001") -> dict:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        return {
            "session_id": str(uuid4()),
            "ocpp_id": ocpp_id,
            "connector_id": 1,
            "vehicle_id": str(uuid4()),
            "driver_id": None,
            "started_at": now - timedelta(minutes=45),
            "ended_at": now - timedelta(minutes=5),
            "energy_delivered_kwh": 21.5,
            "energy_received_kwh": None,
            "cost_total": 4.30,
            "start_soc_percent": 30.0,
            "end_soc_percent": 90.0,
            "source": source,
        }

    def test_favonius_admin_200_returns_sessions(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("favonius_admin"))
        conn.fetchrow = AsyncMock(return_value={"ocpp_id": "acme-berlin-001"})
        rows = [self._session_row(), self._session_row()]
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_charger",
                new_callable=AsyncMock,
                return_value=rows,
            ),
            patch("src.api.main.write_admin_audit_row", new_callable=AsyncMock),
        ):
            response = self._hit(client, "?limit=2")
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert len(body["items"]) == 2
        # Page is full at the requested limit → cursor must be returned.
        assert body["next_cursor"] is not None
        assert "fetched_at" in body

    def test_customer_admin_own_org_200(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetchrow = AsyncMock(return_value={"ocpp_id": "acme-berlin-001"})
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_charger",
                new_callable=AsyncMock,
                return_value=[self._session_row()],
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert len(body["items"]) == 1
        # Partial page → no cursor.
        assert body["next_cursor"] is None

    def test_customer_admin_other_org_403_not_404(self, client, mock_pool):
        pool, _ = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=OTHER_ORG_ID),
            ),
        ):
            response = self._hit(client)
        # 403 (not 404) — never leak depot existence across tenants.
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_DEPOT"

    def test_customer_operator_403_role_gate(self, client, mock_pool):
        """customer_operator is blocked here even though it can read /depots/{}/sessions.

        The role gate matches the sibling fetch_logs action — only customer_admin
        and favonius_admin can use the charger-logs admin workflow.
        """
        _override_user(_user("customer_operator", organization_id=ORG_ID))
        response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_viewer_403(self, client, mock_pool):
        _override_user(_user("viewer"))
        response = self._hit(client)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        assert response.json()["detail"]["error_code"] == "FORBIDDEN_ROLE"

    def test_charger_not_in_depot_404(self, client, mock_pool):
        """Charger UUID exists but belongs to a different depot → 404 CHARGER_NOT_FOUND."""
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetchrow = AsyncMock(return_value=None)
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND
        assert response.json()["detail"]["error_code"] == "CHARGER_NOT_FOUND"

    def test_invalid_charger_uuid_400(self, client, mock_pool):
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        bad_url = f"/admin/depots/{DEPOT_ID}/chargers/not-a-uuid/sessions"
        response = client.get(bad_url, headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_invalid_depot_uuid_400(self, client, mock_pool):
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        bad_url = f"/admin/depots/not-a-uuid/chargers/{CHARGER_ID}/sessions"
        response = client.get(bad_url, headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_from_after_to_400(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetchrow = AsyncMock(return_value={"ocpp_id": "acme-berlin-001"})
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
        ):
            response = self._hit(
                client,
                "?from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z",
            )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_invalid_cursor_400(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetchrow = AsyncMock(return_value={"ocpp_id": "acme-berlin-001"})
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
        ):
            response = self._hit(client, "?cursor=not-base64")
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_cursor_round_trip(self, client, mock_pool):
        """Last item's cursor on page N opens a valid query for page N+1."""
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetchrow = AsyncMock(return_value={"ocpp_id": "acme-berlin-001"})
        rows = [self._session_row()]
        list_mock = AsyncMock(return_value=rows)
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_charger", list_mock
            ),
        ):
            r1 = self._hit(client, "?limit=1")
            cursor = r1.json()["next_cursor"]
            assert cursor is not None
            r2 = self._hit(client, f"?limit=1&cursor={cursor}")
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Second call passed a decoded cursor tuple through to the query.
        assert list_mock.await_args.kwargs["cursor"] is not None

    def test_empty_result(self, client, mock_pool):
        pool, conn = mock_pool
        _override_user(_user("customer_admin", organization_id=ORG_ID))
        conn.fetchrow = AsyncMock(return_value={"ocpp_id": "acme-berlin-001"})
        with (
            patch("src.api.main.db_pools", pool),
            patch(
                "src.api.main.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=_depot_row(organization_id=ORG_ID),
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_charger",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            response = self._hit(client)
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["items"] == []
        assert body["next_cursor"] is None
