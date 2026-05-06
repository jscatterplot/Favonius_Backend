"""Tests for the historical charging-sessions XLSX import endpoint.

Mirrors the bulk-RFID dialog: the frontend parses the spreadsheet and POSTs
one row per call. These tests pin the timezone conversion, identity
resolution chain, idempotent re-upload behavior, and FE-stable error
envelope (Reports → Energy accounting subsection).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException, status as http_status

from src.api.main import (
    _compute_import_row_hash,
    _parse_import_local_timestamp,
    app,
)
from src.security.tenant_mirror import ensure_tenant_mirrored


AUTH_HDR = {"Authorization": "Bearer test-token"}


def _user(org_id: str, role: str = "customer_admin") -> dict:
    """Build a trusted Supabase JWT payload."""
    return {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": role, "organization_id": org_id},
    }


def _override_token(user: dict):
    def override():
        return user

    return override


def _row_payload(**overrides) -> dict:
    """Default valid request body — the example file's first row."""
    payload = {
        "import_batch_id": str(uuid4()),
        "start_time_local": "2026-05-05 12:56",
        "end_time_local": "2026-05-11 01:17",
        "energy_delivered_kwh": 24.044,
        "revenue": 0,
        "id_tag": "ED8503",
        "status": "Finished",
        "transaction_type": "RFID",
        "user_full_name": "HRX Transport",
        "station_owner_full_name": "Gustas Diksa",
    }
    payload.update(overrides)
    return payload


def _depot_row(timezone_name: str = "Europe/Vilnius") -> dict:
    return {"timezone": timezone_name}


def _vehicle_match_row(vehicle_id: str) -> dict:
    return {"vehicle_id": vehicle_id}


def _card_match_row(
    *,
    card_id: str,
    vehicle_id: str | None = None,
    driver_id: str | None = None,
) -> dict:
    return {"card_id": card_id, "vehicle_id": vehicle_id, "driver_id": driver_id}


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class TestPureHelpers:
    """Unit-test the timezone conversion and the dedup hash directly."""

    def test_parse_import_local_timestamp_converts_to_utc(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        # Vilnius is UTC+3 in early May (DST). 12:56 local -> 09:56 UTC.
        result = _parse_import_local_timestamp(
            "2026-05-05 12:56", ZoneInfo("Europe/Vilnius"), field="start_time_local"
        )
        assert result == datetime(2026, 5, 5, 9, 56, tzinfo=timezone.utc)

    def test_parse_import_local_timestamp_accepts_seconds(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        result = _parse_import_local_timestamp(
            "2026-05-05 12:56:30", ZoneInfo("UTC"), field="start_time_local"
        )
        assert result == datetime(2026, 5, 5, 12, 56, 30, tzinfo=timezone.utc)

    def test_parse_import_local_timestamp_rejects_garbage(self):
        from zoneinfo import ZoneInfo

        with pytest.raises(HTTPException) as exc_info:
            _parse_import_local_timestamp(
                "not-a-date", ZoneInfo("UTC"), field="start_time_local"
            )
        assert exc_info.value.status_code == http_status.HTTP_400_BAD_REQUEST
        assert exc_info.value.detail["error_code"] == "INVALID_TIMESTAMP"
        assert exc_info.value.detail["field"] == "start_time_local"

    def test_parse_import_local_timestamp_rejects_empty(self):
        from zoneinfo import ZoneInfo

        with pytest.raises(HTTPException) as exc_info:
            _parse_import_local_timestamp(
                "", ZoneInfo("UTC"), field="start_time_local"
            )
        assert exc_info.value.detail["error_code"] == "MISSING_REQUIRED_FIELD"

    def test_compute_import_row_hash_is_deterministic(self):
        from datetime import datetime, timezone

        depot_id = str(uuid4())
        ts = datetime(2026, 5, 5, 9, 56, tzinfo=timezone.utc)
        a = _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
            energy_delivered_kwh=24.044,
            revenue=0,
        )
        b = _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
            energy_delivered_kwh=24.044,
            revenue=0,
        )
        assert a == b
        assert len(a) == 64

    def test_compute_import_row_hash_changes_on_any_input(self):
        from datetime import datetime, timezone

        depot_id = str(uuid4())
        ts = datetime(2026, 5, 5, 9, 56, tzinfo=timezone.utc)
        base = _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
            energy_delivered_kwh=24.044,
            revenue=0,
        )
        # different id_tag
        assert base != _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="OTHER",
            energy_delivered_kwh=24.044,
            revenue=0,
        )
        # different energy
        assert base != _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
            energy_delivered_kwh=24.045,
            revenue=0,
        )
        # different revenue
        assert base != _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
            energy_delivered_kwh=24.044,
            revenue=1.5,
        )


# --------------------------------------------------------------------------- #
# Endpoint integration with mocked DB
# --------------------------------------------------------------------------- #


def _patch_endpoint_deps(pool):
    """Common patch context: db_pools + verify_depot_access bypass."""
    return (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
    )


def _set_fetchrow_sequence(conn, *rows):
    """Configure conn.fetchrow to return ``rows`` in order across awaits."""
    conn.fetchrow = AsyncMock(side_effect=list(rows))


class TestHistoricalChargingSessionImport:
    """End-to-end behavior of POST /admin/depots/{id}/charging-sessions/import."""

    def _setup(self, mock_db_pool, *, depot_row, vehicle_row, card_row):
        pool, conn = mock_db_pool
        # Ordered fetchrow calls inside the endpoint:
        #   1) sites lookup (timezone + org check)
        #   2) vehicles lookup by id_tag
        #   3) rfid_cards lookup by id_tag
        _set_fetchrow_sequence(conn, depot_row, vehicle_row, card_row)
        return pool, conn

    def test_finished_row_inserts_with_utc_conversion_and_card_match(
        self, client, mock_db_pool
    ):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        card_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,  # no direct vehicle match — falls through to card
            card_row=_card_match_row(
                card_id=card_id, vehicle_id=vehicle_id, driver_id=None
            ),
        )
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["session_id"] == session_id
        assert body["matched"]["card_id"] == card_id
        assert body["matched"]["vehicle_id"] == vehicle_id
        assert body["matched"]["driver_id"] is None

        # Validate the INSERT bind values: placeholder station_id, UTC times,
        # source via SQL literal ('import'), depot_id as site_id. fetchval is
        # called as fetchval(query, *bind_values), so args[0] is the SQL
        # string; bind values start at args[1].
        bind = conn.fetchval.await_args.args[1:]
        from datetime import datetime, timezone

        assert bind[0] == f"imported:{depot_id}"  # $1 placeholder station_id
        assert bind[1] == vehicle_id  # $2 vehicle_id resolved from card
        assert bind[2] == "ED8503"  # $3 id_token = raw id_tag
        assert bind[3] is None  # $4 driver_id
        assert bind[4] == card_id  # $5 card_id
        # start_time_local 12:56 Vilnius (UTC+3 DST) -> 09:56 UTC
        assert bind[5] == datetime(2026, 5, 5, 9, 56, tzinfo=timezone.utc)  # $6
        # end_time_local 2026-05-11 01:17 Vilnius -> 22:17 UTC on 2026-05-10
        assert bind[6] == datetime(2026, 5, 10, 22, 17, tzinfo=timezone.utc)  # $7
        assert float(bind[7]) == pytest.approx(24.044)  # $8 energy_delivered_kwh
        assert float(bind[8]) == 0.0  # $9 revenue -> cost_total
        assert bind[9] == depot_id  # $10 site_id
        assert bind[14] == "Finished"  # $15 import_status

    def test_charging_status_stores_null_end_time(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
        )
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(status="Charging"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        bind = conn.fetchval.await_args.args[1:]
        assert bind[6] is None  # $7 end_time must be NULL for "Charging" rows
        assert bind[14] == "Charging"  # $15 raw import_status preserved

    def test_vehicle_id_tag_match_skips_card_lookup(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("UTC"),
            vehicle_row=_vehicle_match_row(vehicle_id),
            card_row=None,  # would 500 if reached — confirms we short-circuit
        )
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["matched"]["vehicle_id"] == vehicle_id
        assert body["matched"]["card_id"] is None
        assert body["matched"]["driver_id"] is None
        # Only two fetchrow calls used: sites + vehicles. The third was queued
        # via side_effect but never awaited.
        assert conn.fetchrow.await_count == 2

    def test_unmatched_id_tag_inserts_with_null_identity_but_keeps_id_token(
        self, client, mock_db_pool
    ):
        """An "Opel Mokka" cell never matches; row still imports for accounting."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
        )
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(id_tag="Opel Mokka"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["matched"]["vehicle_id"] is None
        assert body["matched"]["card_id"] is None
        assert body["matched"]["driver_id"] is None

        bind = conn.fetchval.await_args.args[1:]
        assert bind[2] == "Opel Mokka"  # $3 id_token preserved
        assert bind[1] is None  # $2 vehicle_id NULL
        assert bind[4] is None  # $5 card_id NULL

    def test_duplicate_row_returns_409_duplicate_session(self, client, mock_db_pool):
        """Re-uploading the same file row hits the partial unique index."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
        )
        err = asyncpg.UniqueViolationError("duplicate")
        err.constraint_name = "charging_sessions_import_dedup_idx"
        conn.fetchval = AsyncMock(side_effect=err)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(),
            )

        assert response.status_code == http_status.HTTP_409_CONFLICT
        body = response.json()
        # The bulk-import dialog keys off this stable code to count duplicates.
        assert body["error_code"] == "DUPLICATE_SESSION"

    def test_invalid_timestamp_returns_400(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
        )
        conn.fetchval = AsyncMock()

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(start_time_local="garbage"),
            )

        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert body["error_code"] == "INVALID_TIMESTAMP"
        # No INSERT issued when validation fails.
        conn.fetchval.assert_not_awaited()

    def test_invalid_status_rejected_by_pydantic(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(status="Pending"),
            )

        # Literal[...] mismatch routes through the app-wide
        # validation_exception_handler, which standardizes to 400 + VALIDATION_ERROR.
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    def test_negative_energy_rejected_by_pydantic(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(energy_delivered_kwh=-1),
            )

        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    def test_end_before_start_returns_400(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
        )
        conn.fetchval = AsyncMock()

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(
                    start_time_local="2026-05-05 12:56",
                    end_time_local="2026-05-05 09:00",
                ),
            )

        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert body["error_code"] == "INVALID_TIMESTAMP"
        conn.fetchval.assert_not_awaited()

    def test_non_customer_admin_returns_403(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _user(org_id, role="customer_operator")
        )
        pool, _ = mock_db_pool

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(),
            )

        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_mismatched_org_returns_404(self, client, mock_db_pool):
        """sites lookup includes organization_id filter — wrong org -> 404."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=None,  # no row returned -> 404 path
            vehicle_row=None,
            card_row=None,
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(),
            )

        assert response.status_code == http_status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "DEPOT_NOT_FOUND"

    # ---------------------------------------------------------------------- #
    # rfid_label resolution (TOKS export flow)
    # ---------------------------------------------------------------------- #

    def test_rfid_label_matches_card_label_case_insensitive(self, client, mock_db_pool):
        """rfid_label hit on rfid_cards.label skips all id_tag lookups."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        card_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = mock_db_pool
        # fetchrow sequence: 1) sites  2) rfid_cards.label hit (stops here)
        _set_fetchrow_sequence(
            conn,
            _depot_row("Europe/Vilnius"),
            _card_match_row(card_id=card_id, vehicle_id=vehicle_id, driver_id=None),
        )
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(rfid_label="Opel Mokka", id_tag="ED8503"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["matched"]["card_id"] == card_id
        assert body["matched"]["vehicle_id"] == vehicle_id
        assert body["matched"]["driver_id"] is None
        # Label matched on the 2nd fetchrow — no vehicle/card id_tag queries ran
        assert conn.fetchrow.await_count == 2
        # id_token stores rfid_label (preferred over id_tag)
        bind = conn.fetchval.await_args.args[1:]
        assert bind[2] == "Opel Mokka"  # $3 id_token

    def test_rfid_label_miss_falls_back_to_id_tag_path(self, client, mock_db_pool):
        """When rfid_label finds no card, resolution continues with id_tag."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = mock_db_pool
        # fetchrow sequence: 1) sites  2) label miss  3) vehicle hit
        _set_fetchrow_sequence(
            conn,
            _depot_row("UTC"),
            None,  # rfid_cards.label -> no match
            _vehicle_match_row(vehicle_id),  # vehicles.id_tag -> hit
        )
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(rfid_label="Unknown Label", id_tag="ED8503"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["matched"]["vehicle_id"] == vehicle_id
        assert body["matched"]["card_id"] is None
        # id_token stores rfid_label (it was provided)
        bind = conn.fetchval.await_args.args[1:]
        assert bind[2] == "Unknown Label"  # $3 id_token

    def test_rfid_label_only_no_match_stores_unmatched(self, client, mock_db_pool):
        """rfid_label with no match and no id_tag → 201, all identity fields null."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = mock_db_pool
        # fetchrow sequence: 1) sites  2) label miss  (no id_tag → stops)
        _set_fetchrow_sequence(conn, _depot_row("UTC"), None)
        conn.fetchval = AsyncMock(return_value=session_id)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json={
                    **_row_payload(rfid_label="Opel Mokka"),
                    "id_tag": None,  # explicitly absent
                },
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["matched"]["vehicle_id"] is None
        assert body["matched"]["card_id"] is None
        assert body["matched"]["driver_id"] is None
        bind = conn.fetchval.await_args.args[1:]
        assert bind[2] == "Opel Mokka"  # $3 id_token = rfid_label

    def test_neither_rfid_label_nor_id_tag_returns_validation_error(
        self, client, mock_db_pool
    ):
        """At least one of rfid_label / id_tag must be provided."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool

        payload = _row_payload()
        del payload["id_tag"]  # omit id_tag; rfid_label absent → validator fires

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=payload,
            )

        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert response.json()["error_code"] == "VALIDATION_ERROR"
