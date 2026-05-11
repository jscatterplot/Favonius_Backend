"""Tests for the historical charging-sessions XLSX import endpoint.

Mirrors the bulk-RFID dialog: the frontend parses the spreadsheet and POSTs
one row per call. These tests pin the timezone conversion, identity
resolution chain, idempotent re-upload behavior, and FE-stable error
envelope (Reports → Energy accounting subsection).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException, status as http_status

from src.api.charging_import import StaticPriceSource, invalidate_price_cache
from src.api.main import (
    _compute_import_row_hash,
    _platform_import_hash_token,
    _parse_import_local_timestamp,
    HistoricalSessionImport,
    app,
    get_price_source,
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


def _depot_row(
    timezone_name: str = "Europe/Vilnius",
    *,
    organization_id: str | None = None,
) -> dict:
    """Build a sites row matching the columns _get_site_metadata reads."""
    return {
        "depot_id": str(uuid4()),
        "organization_id": organization_id or str(uuid4()),
        "timezone": timezone_name,
        "tariff_config": None,
    }


def _vehicle_match_row(vehicle_id: str) -> dict:
    return {"vehicle_id": vehicle_id}


def _card_match_row(
    *,
    card_id: str,
    vehicle_id: str | None = None,
    driver_id: str | None = None,
) -> dict:
    return {"card_id": card_id, "vehicle_id": vehicle_id, "driver_id": driver_id}


def _upsert_row(session_id: str, *, was_new: bool = True) -> dict:
    """RETURNING row from the UPSERT — session_id + xmax-derived was_new."""
    return {"session_id": session_id, "was_new": was_new}


@pytest.fixture(autouse=True)
def reset_import_endpoint_state():
    """Reset module-level caches and force a no-op PriceSource for every test.

    The endpoint caches `sites` metadata and prices at module scope so a single
    import-batch only queries each (depot, hour) once. Tests need a clean slate
    between cases. StaticPriceSource(None) makes cost_total fall back to the
    request's `revenue` field — matches the legacy behavior these tests pin.
    """
    from src.api import main as api_main

    api_main._site_metadata_cache.clear()
    api_main._site_metadata_locks.clear()
    invalidate_price_cache()

    app.dependency_overrides[get_price_source] = lambda: StaticPriceSource(None)
    yield
    app.dependency_overrides.clear()
    api_main._site_metadata_cache.clear()
    api_main._site_metadata_locks.clear()
    invalidate_price_cache()


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
        )
        b = _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
        )
        assert a == b
        assert len(a) == 64

    def test_compute_import_row_hash_changes_on_canonical_inputs(self):
        """Hash must differ when ANY of (depot_id, start_time, id_tag) differs."""
        from datetime import datetime, timezone

        depot_id = str(uuid4())
        other_depot = str(uuid4())
        ts = datetime(2026, 5, 5, 9, 56, tzinfo=timezone.utc)
        later = datetime(2026, 5, 5, 9, 57, tzinfo=timezone.utc)
        base = _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=ts,
            id_tag="ED8503",
        )
        # different depot
        assert base != _compute_import_row_hash(
            depot_id=other_depot, start_time_utc=ts, id_tag="ED8503"
        )
        # different start_time
        assert base != _compute_import_row_hash(
            depot_id=depot_id, start_time_utc=later, id_tag="ED8503"
        )
        # different id_tag
        assert base != _compute_import_row_hash(
            depot_id=depot_id, start_time_utc=ts, id_tag="OTHER"
        )

    def test_compute_import_row_hash_is_stable_when_energy_or_revenue_change(self):
        """Energy / revenue are NOT in the canonical form — corrected re-uploads
        merge into the same row via the runtime UPSERT-with-fill-nulls path.

        Pins migration 036's promise: a customer who re-uploads an XLSX with a
        corrected metering value should not create a duplicate row.
        """
        from datetime import datetime, timezone

        depot_id = str(uuid4())
        ts = datetime(2026, 5, 5, 9, 56, tzinfo=timezone.utc)
        base = _compute_import_row_hash(
            depot_id=depot_id, start_time_utc=ts, id_tag="ED8503"
        )
        # The function signature deliberately does not accept energy / revenue;
        # tests that previously varied those values must merge into the same
        # row at the UPSERT layer, not produce different hashes.
        assert base == _compute_import_row_hash(
            depot_id=depot_id, start_time_utc=ts, id_tag="ED8503"
        )

    def test_platform_import_hash_token_changes_with_persisted_content(self):
        """Platform-start dedup token distinguishes rows by persisted fields.

        Only fields present in charging_sessions are part of the canonical
        form (end_time, import_status, import_user_full_name,
        import_station_owner) — transaction_type is excluded because it isn't
        persisted and migration 036 must be able to reconstruct the same token
        from DB columns alone.
        """
        from datetime import datetime, timezone
        from uuid import uuid4

        common = {
            "import_batch_id": uuid4(),
            "start_time_local": "2026-05-05 12:56",
            "end_time_local": "2026-05-11 01:17",
            "energy_delivered_kwh": 24.044,
            "revenue": 0.0,
            "id_tag": None,
            "rfid_label": None,
            "status": "Finished",
            "transaction_type": "Dashboard",
            "user_full_name": "HRX Transport",
            "station_owner_full_name": "Gustas Diksa",
        }
        request_base = HistoricalSessionImport(**common)
        end_time = datetime(2026, 5, 10, 22, 17, tzinfo=timezone.utc)
        token_base = _platform_import_hash_token(request_base, end_time_utc=end_time)

        # status / user_name / station_owner / end_time each shift the token.
        for override in (
            {"status": "Charging"},
            {"user_full_name": "Other Transport"},
            {"station_owner_full_name": "Other Owner"},
        ):
            request_other = HistoricalSessionImport(**{**common, **override})
            assert (
                _platform_import_hash_token(request_other, end_time_utc=end_time)
                != token_base
            ), f"override {override} must change the token"

        # end_time shifts the token too.
        other_end = datetime(2026, 5, 10, 23, 0, tzinfo=timezone.utc)
        assert (
            _platform_import_hash_token(request_base, end_time_utc=other_end)
            != token_base
        )

        # transaction_type is NOT in the canonical form (migration 036).
        request_other_txn = HistoricalSessionImport(
            **{**common, "transaction_type": "IOS"}
        )
        assert (
            _platform_import_hash_token(request_other_txn, end_time_utc=end_time)
            == token_base
        )

    def test_platform_import_hash_token_handles_pipe_characters_without_colliding(self):
        """Free-text fields containing pipes should not collapse into same dedup token."""
        from datetime import datetime, timezone
        from uuid import uuid4

        common = {
            "import_batch_id": uuid4(),
            "start_time_local": "2026-05-05 12:56",
            "end_time_local": "2026-05-11 01:17",
            "energy_delivered_kwh": 24.044,
            "revenue": 0.0,
            "id_tag": None,
            "rfid_label": None,
            "status": "Finished",
            "transaction_type": "Dashboard",
        }
        request_a = HistoricalSessionImport(
            **{**common, "user_full_name": "A|", "station_owner_full_name": "B"}
        )
        request_b = HistoricalSessionImport(
            **{**common, "user_full_name": "A", "station_owner_full_name": "|B"}
        )
        end_time = datetime(2026, 5, 10, 22, 17, tzinfo=timezone.utc)

        token_a = _platform_import_hash_token(request_a, end_time_utc=end_time)
        token_b = _platform_import_hash_token(request_b, end_time_utc=end_time)
        assert token_a != token_b


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


def _upsert_bind(conn) -> tuple:
    """Bind values passed to the UPSERT (the final fetchrow call)."""
    return conn.fetchrow.await_args_list[-1].args[1:]


def _upsert_sql(conn) -> str:
    """SQL string passed to the UPSERT (the final fetchrow call)."""
    return conn.fetchrow.await_args_list[-1].args[0]


def _upsert_was_called(conn) -> bool:
    """True iff fetchrow was ever invoked with the UPSERT SQL."""
    return any(
        call.args and "INSERT INTO charging_sessions" in str(call.args[0])
        for call in conn.fetchrow.await_args_list
    )


class TestHistoricalChargingSessionImport:
    """End-to-end behavior of POST /admin/depots/{id}/charging-sessions/import."""

    def _setup(
        self,
        mock_db_pool,
        *,
        depot_row,
        vehicle_row,
        card_row,
        upsert_row=None,
        session_id: str | None = None,
        org_id: str | None = None,
    ):
        """Configure the conn fetchrow sequence for the endpoint's full path.

        Ordered fetchrow calls inside the endpoint:
          1) sites lookup (cached in _site_metadata_cache after first hit)
          2) vehicles lookup by id_tag (skipped for platform-initiated rows)
          3) rfid_cards lookup by id_tag (skipped on early hit)
          4) UPSERT into charging_sessions returning (session_id, was_new)

        ``org_id`` patches the depot_row so its ``organization_id`` matches the
        caller's JWT org claim; tests that omit it use whatever random org_id
        ``_depot_row`` generated and exercise the 404 path.
        """
        pool, conn = mock_db_pool
        if depot_row is not None and org_id is not None:
            depot_row = {**depot_row, "organization_id": org_id}
        if upsert_row is None:
            sid = session_id or str(uuid4())
            upsert_row = _upsert_row(sid)
        _set_fetchrow_sequence(conn, depot_row, vehicle_row, card_row, upsert_row)
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
            session_id=session_id,
            org_id=org_id,
        )

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
        bind = _upsert_bind(conn)
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

    def test_insert_targets_columns_added_by_migration_032(
        self, client, mock_db_pool
    ):
        """Pin the INSERT contract to the columns migration 032 backfills.

        Production traceback that motivated migration 032:

            asyncpg.exceptions.UndefinedColumnError: column "energy_delivered_kwh"
                of relation "charging_sessions" does not exist

        This test fails loudly if someone drops these columns from the INSERT
        or renames them out of sync with migrations/. It is the first line of
        defence against the schema drift between
        src/websocket_handler/timescale_schema.py (which originally created
        the columns) and migrations/ (which now must own them).
        """
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("UTC"),
            vehicle_row=None,
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        sql = _upsert_sql(conn)
        # Both columns must appear in the column list, not just in a comment.
        # Extract the column list to be robust to whitespace.
        col_list_start = sql.index("INSERT INTO charging_sessions (") + len(
            "INSERT INTO charging_sessions ("
        )
        col_list_end = sql.index(")", col_list_start)
        col_list = sql[col_list_start:col_list_end]
        cols = {c.strip() for c in col_list.split(",")}
        assert "energy_delivered_kwh" in cols
        assert "cost_total" in cols

    def test_charging_status_persists_end_time_when_supplied(
        self, client, mock_db_pool
    ):
        """Non-Finished statuses persist end_time when the FE supplies one.

        The export emits multiple terminal/non-terminal statuses; the only
        timestamp invariant we enforce is end >= start (covered separately).
        Status semantics live in import_status (free text), not in any
        backend state machine.
        """
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(status="Charging"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        from datetime import datetime, timezone

        bind = _upsert_bind(conn)
        # end_time_local 2026-05-11 01:17 Vilnius (UTC+3 DST) -> 22:17 UTC on 2026-05-10
        assert bind[6] == datetime(2026, 5, 10, 22, 17, tzinfo=timezone.utc)  # $7
        assert bind[14] == "Charging"  # $15 raw import_status preserved

    def test_connected_stopped_by_ev_status_persists_end_time(
        self, client, mock_db_pool
    ):
        """Non-Literal status from the upstream export round-trips end_time."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(status="ConnectedStoppedByEv"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        from datetime import datetime, timezone

        bind = _upsert_bind(conn)
        assert bind[6] == datetime(2026, 5, 10, 22, 17, tzinfo=timezone.utc)  # $7
        assert bind[14] == "ConnectedStoppedByEv"  # $15 raw import_status preserved

    def test_status_with_no_end_time_local_stores_null_end_time(
        self, client, mock_db_pool
    ):
        """Without end_time_local, end_time is NULL regardless of status."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )

        payload = _row_payload(status="Charging")
        payload["end_time_local"] = None  # FE may suppress for ongoing sessions

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=payload,
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        bind = _upsert_bind(conn)
        assert bind[6] is None  # $7 end_time NULL when omitted
        assert bind[14] == "Charging"

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
            session_id=session_id,
            org_id=org_id,
        )

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
        # sites + identity-hit + UPSERT = 3 fetchrow calls; the second identity
        # query (card or vehicle, depending on path) is short-circuited.
        assert conn.fetchrow.await_count == 3

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
            session_id=session_id,
            org_id=org_id,
        )

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

        bind = _upsert_bind(conn)
        assert bind[2] == "Opel Mokka"  # $3 id_token preserved
        assert bind[1] is None  # $2 vehicle_id NULL
        assert bind[4] is None  # $5 card_id NULL

    def test_re_uploading_same_row_merges_and_returns_was_new_false(
        self, client, mock_db_pool
    ):
        """Re-uploading the same logical row merges via UPSERT-with-fill-nulls.

        Previously this surfaced as a 409 DUPLICATE_SESSION. Since migration 036
        and the runtime UPSERT switch, the API returns 201 with the existing
        session_id and `was_new = false`; the bulk-import dialog reads
        `was_new` to surface "N merged" vs "N new" counts to operators.
        """
        depot_id = str(uuid4())
        org_id = str(uuid4())
        existing_session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            upsert_row=_upsert_row(existing_session_id, was_new=False),
            org_id=org_id,
        )

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
        assert body["session_id"] == existing_session_id
        assert body["was_new"] is False

        # The UPSERT SQL must include the ON CONFLICT clause; this pins the
        # fill-nulls behavior at the SQL boundary so a future refactor can't
        # silently drop it.
        sql = _upsert_sql(conn)
        assert "ON CONFLICT" in sql
        assert "DO UPDATE SET" in sql
        assert "COALESCE(charging_sessions" in sql

    def test_unique_violation_outside_dedup_index_still_maps_to_409(
        self, client, mock_db_pool
    ):
        """A non-dedup unique-violation (e.g. id_tag clash) still surfaces as 409.

        The endpoint's UPSERT only swallows conflicts on the dedup index. Any
        other UniqueViolationError from a sibling write must still propagate
        through ``_handle_identity_unique_violation``.
        """
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            org_id=org_id,
        )
        # Replace the upsert_row in the side_effect with an exception so the
        # UPSERT call itself raises. Constraint name is NOT the dedup index, so
        # the mapper picks the generic CONFLICT code rather than DUPLICATE_SESSION.
        err = asyncpg.UniqueViolationError("conflict")
        err.constraint_name = "vehicles_id_tag_unique"
        conn.fetchrow = AsyncMock(
            side_effect=[
                _depot_row("Europe/Vilnius", organization_id=org_id),
                None,  # vehicle miss
                None,  # rfid miss
                err,  # UPSERT raises
            ]
        )

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
        assert body["error_code"] == "DUPLICATE_ID_TAG"

    def test_invalid_timestamp_returns_400(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            org_id=org_id,
        )

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
        # No UPSERT issued when validation fails.
        assert not _upsert_was_called(conn)

    def test_arbitrary_status_string_round_trips_to_import_status(
        self, client, mock_db_pool
    ):
        """status is open-text (no closed Literal) so any upstream code persists.

        The source export has emitted Charging / Finished / ConnectedStoppedByEv
        in the wild. Refusing unknown codes at the validation layer would
        silently drop rows from the bulk-import dialog with VALIDATION_ERROR
        instead of recording the truth — see commit history for migration 032.
        """
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("Europe/Vilnius"),
            vehicle_row=None,
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=_row_payload(status="Faulted"),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        bind = _upsert_bind(conn)
        assert bind[14] == "Faulted"  # $15 raw status persisted verbatim

    def test_blank_status_rejected_by_pydantic(self, client, mock_db_pool):
        """Empty string is still rejected; only known-bad payloads are kept out."""
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
                json=_row_payload(status=""),
            )

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
            org_id=org_id,
        )

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
        assert not _upsert_was_called(conn)

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
        # fetchrow sequence: 1) sites  2) rfid_cards.label hit  3) UPSERT
        _set_fetchrow_sequence(
            conn,
            _depot_row("Europe/Vilnius", organization_id=org_id),
            _card_match_row(card_id=card_id, vehicle_id=vehicle_id, driver_id=None),
            _upsert_row(session_id),
        )

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
        # sites + identity-hit + UPSERT = 3 fetchrow calls; the second identity
        # query (card or vehicle, depending on path) is short-circuited.
        assert conn.fetchrow.await_count == 3
        # id_token stores rfid_label (preferred over id_tag)
        bind = _upsert_bind(conn)
        assert bind[2] == "Opel Mokka"  # $3 id_token

    def test_rfid_label_miss_falls_back_to_id_tag_path(self, client, mock_db_pool):
        """When rfid_label finds no card, resolution continues with id_tag."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = mock_db_pool
        # fetchrow sequence: 1) sites  2) label miss  3) vehicle hit  4) UPSERT
        _set_fetchrow_sequence(
            conn,
            _depot_row("UTC", organization_id=org_id),
            None,  # rfid_cards.label -> no match
            _vehicle_match_row(vehicle_id),  # vehicles.id_tag -> hit
            _upsert_row(session_id),
        )

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
        bind = _upsert_bind(conn)
        assert bind[2] == "Unknown Label"  # $3 id_token

    def test_rfid_label_only_no_match_stores_unmatched(self, client, mock_db_pool):
        """rfid_label with no match and no id_tag → 201, all identity fields null."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = mock_db_pool
        # fetchrow sequence: 1) sites  2) label miss  3) UPSERT
        _set_fetchrow_sequence(
            conn,
            _depot_row("UTC", organization_id=org_id),
            None,
            _upsert_row(session_id),
        )

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
        bind = _upsert_bind(conn)
        assert bind[2] == "Opel Mokka"  # $3 id_token = rfid_label

    def test_neither_rfid_label_nor_id_tag_imports_as_platform_started(
        self, client, mock_db_pool
    ):
        """Rows without RFID identifiers are imported as platform-initiated."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("UTC"),
            vehicle_row=None,
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )

        payload = _row_payload(id_tag=None, transaction_type="IOS")

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=payload,
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["session_id"] == session_id
        assert body["matched"]["vehicle_id"] is None
        assert body["matched"]["card_id"] is None
        assert body["matched"]["driver_id"] is None

        bind = _upsert_bind(conn)
        assert bind[2] == "platform-start"  # $3 id_token marker for platform starts

    def test_literal_platform_start_identifier_is_not_treated_as_platform_initiated(
        self, client, mock_db_pool
    ):
        """Literal id_tag value should keep regular dedup hashing behavior."""
        from datetime import datetime, timezone

        depot_id = str(uuid4())
        org_id = str(uuid4())
        session_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        pool, conn = self._setup(
            mock_db_pool,
            depot_row=_depot_row("UTC"),
            vehicle_row=_vehicle_match_row(vehicle_id),
            card_row=None,
            session_id=session_id,
            org_id=org_id,
        )
        payload = _row_payload(id_tag="platform-start")

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/charging-sessions/import",
                headers=AUTH_HDR,
                json=payload,
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["matched"]["vehicle_id"] == vehicle_id
        bind = _upsert_bind(conn)
        assert bind[2] == "platform-start"
        # Literal "platform-start" id_tag is treated as a regular id, NOT as a
        # platform-initiated row, so the hash uses the raw token (no platform
        # envelope) and is invariant under energy/revenue corrections.
        expected_hash = _compute_import_row_hash(
            depot_id=depot_id,
            start_time_utc=datetime(2026, 5, 5, 12, 56, tzinfo=timezone.utc),
            id_tag="platform-start",
        )
        assert bind[11] == expected_hash
