"""Unit tests for GET /depots/{id}/data-graph.

Covers OCPP, ENTSO-E, Kempower, and Navirec status transitions, plus the
absence rules (node omitted when source not configured).

When a new data source is wired into _build_data_graph, add a test class
here that covers: ok / warn / danger / absent (not configured) states.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.security.tenant_mirror import ensure_tenant_mirrored

DEPOT_ID = str(uuid4())
NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def admin_auth():
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


@pytest.fixture
def client():
    return TestClient(app)


def _make_pool(
    *,
    station_ids: list[str] | None = None,
    vehicle_ids: list[str] | None = None,
    conn_rows: list[dict] | None = None,
    connector_statuses: dict | None = None,
    entsoe_latest: datetime | None = None,
    navirec_latest: datetime | None = None,
) -> tuple[MagicMock, MagicMock, dict]:
    """Build (static_pool, ts_pool, connector_statuses) for data-graph tests.

    Returns the two pools separately so patches use
    ``MagicMock(static=static_pool, ts=ts_pool)`` — bypassing a wrapper
    object that would intercept acquire() calls.

    The ts pool's fetchrow routes by SQL keyword so tests are insensitive to
    asyncio.gather call ordering.
    """
    _station_rows = [{"station_id": s} for s in (station_ids or ["ocpp-001"])]
    _vehicle_rows = [{"id": uuid4()} for _ in (vehicle_ids or ["v1"])]
    _conn_rows = conn_rows if conn_rows is not None else []

    # ── Static pool: three sequential fetch calls ──────────────────────────
    static_conn = AsyncMock()
    static_conn.fetch.side_effect = [_station_rows, _vehicle_rows, _conn_rows]
    static_pool = MagicMock()
    static_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=static_conn)
    static_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # ── TS pool: fetchrow routed by SQL keyword ────────────────────────────
    _entsoe_latest = entsoe_latest
    _navirec_latest = navirec_latest

    async def _ts_fetchrow(query: str, *_args):
        if "electricity_prices" in query:
            return {"latest_time": _entsoe_latest}
        if "vehicle_telemetry" in query:
            return {"latest_time": _navirec_latest}
        return None

    ts_conn = AsyncMock()
    ts_conn.fetchrow = AsyncMock(side_effect=_ts_fetchrow)
    ts_pool = MagicMock()
    ts_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=ts_conn)
    ts_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    _connector_statuses = connector_statuses if connector_statuses is not None else {
        "ocpp-001": {"ocpp_status": "Available", "last_interaction_at": NOW}
    }

    return static_pool, ts_pool, _connector_statuses


def _patch_db(static_pool, ts_pool, connector_statuses, *, bidding_zone=None):
    """Return a context-manager stack for the three standard patches."""
    import contextlib
    from unittest.mock import patch as _patch

    @contextlib.contextmanager
    def _ctx():
        with _patch("src.api.main.db_pools", MagicMock(static=static_pool, ts=ts_pool)), \
             _patch("src.api.main.db_queries.resolve_bidding_zone",
                    AsyncMock(return_value=bidding_zone)), \
             _patch("src.api.main.db_queries.latest_connector_status_by_stations",
                    AsyncMock(return_value=connector_statuses)):
            yield

    return _ctx()


class TestOCPPNode:
    """OCPP charger network health transitions."""

    def test_ocpp_ok_when_all_chargers_available(self, client):
        cs = {"ocpp-001": {"ocpp_status": "Available", "last_interaction_at": NOW}}
        sp, tp, _ = _make_pool(connector_statuses=cs)
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.status_code == 200
        ocpp = next(n for n in resp.json()["nodes"] if n["id"] == "src:ocpp")
        assert ocpp["status"] == "ok"
        assert ocpp["description"] is None

    def test_ocpp_warn_when_some_chargers_faulted(self, client):
        cs = {
            "ocpp-001": {"ocpp_status": "Faulted", "last_interaction_at": NOW},
            "ocpp-002": {"ocpp_status": "Available", "last_interaction_at": NOW},
        }
        sp, tp, _ = _make_pool(station_ids=["ocpp-001", "ocpp-002"], connector_statuses=cs)
        # override fetch side_effect for two stations
        sp.acquire.return_value.__aenter__.return_value.fetch.side_effect = [
            [{"station_id": "ocpp-001"}, {"station_id": "ocpp-002"}],
            [],
            [],
        ]
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.status_code == 200
        ocpp = next(n for n in resp.json()["nodes"] if n["id"] == "src:ocpp")
        assert ocpp["status"] == "warn"
        assert "1 of 2" in ocpp["description"]

    def test_ocpp_danger_when_all_chargers_faulted(self, client):
        cs = {"ocpp-001": {"ocpp_status": "Faulted", "last_interaction_at": NOW}}
        sp, tp, _ = _make_pool(connector_statuses=cs)
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.status_code == 200
        ocpp = next(n for n in resp.json()["nodes"] if n["id"] == "src:ocpp")
        assert ocpp["status"] == "danger"

    def test_ocpp_paused_when_no_chargers_configured(self, client):
        sp, tp, _ = _make_pool(connector_statuses={})
        sp.acquire.return_value.__aenter__.return_value.fetch.side_effect = [[], [], []]
        with _patch_db(sp, tp, {}):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.status_code == 200
        ocpp = next(n for n in resp.json()["nodes"] if n["id"] == "src:ocpp")
        assert ocpp["status"] == "paused"


class TestENTSOENode:
    """ENTSO-E price feed health transitions."""

    def test_entsoe_absent_when_no_bidding_zone(self, client):
        sp, tp, cs = _make_pool()
        with _patch_db(sp, tp, cs, bidding_zone=None):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.status_code == 200
        assert "src:entsoe" not in [n["id"] for n in resp.json()["nodes"]]

    def test_entsoe_ok_when_prices_fresh(self, client):
        sp, tp, cs = _make_pool(entsoe_latest=NOW - timedelta(hours=10))
        with _patch_db(sp, tp, cs, bidding_zone="10YLT"):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        entsoe = next(n for n in resp.json()["nodes"] if n["id"] == "src:entsoe")
        assert entsoe["status"] == "ok"
        assert entsoe["description"] is None

    def test_entsoe_warn_when_prices_26h_old(self, client):
        sp, tp, cs = _make_pool(entsoe_latest=NOW - timedelta(hours=26))
        with _patch_db(sp, tp, cs, bidding_zone="10YLT"):
            with patch("src.api.main.datetime") as mock_dt:
                mock_dt.now.return_value = NOW
                resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        entsoe = next(n for n in resp.json()["nodes"] if n["id"] == "src:entsoe")
        assert entsoe["status"] == "warn"
        assert "26h" in entsoe["description"]

    def test_entsoe_danger_when_prices_50h_old(self, client):
        sp, tp, cs = _make_pool(entsoe_latest=NOW - timedelta(hours=50))
        with _patch_db(sp, tp, cs, bidding_zone="10YLT"):
            with patch("src.api.main.datetime") as mock_dt:
                mock_dt.now.return_value = NOW
                resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        entsoe = next(n for n in resp.json()["nodes"] if n["id"] == "src:entsoe")
        assert entsoe["status"] == "danger"

    def test_entsoe_danger_when_no_prices_at_all(self, client):
        sp, tp, cs = _make_pool(entsoe_latest=None)
        with _patch_db(sp, tp, cs, bidding_zone="10YLT"):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        entsoe = next(n for n in resp.json()["nodes"] if n["id"] == "src:entsoe")
        assert entsoe["status"] == "danger"


class TestKempowerNode:
    """Self-serve data-source connection (Kempower) health transitions."""

    def _pool_with_connection(
        self, last_job: str | None, *, paused: bool = False, error: str | None = None
    ) -> tuple[MagicMock, MagicMock, dict]:
        cs = {"ocpp-001": {"ocpp_status": "Available", "last_interaction_at": NOW}}
        sp, tp, _ = _make_pool(connector_statuses=cs)
        conn_row = {
            "id": str(uuid4()),
            "provider_key": "kempower",
            "display_name": "Kempower ChargEye",
            "connection_status": "paused" if paused else "active",
            "sync_interval_minutes": 60,
            "last_run_at": NOW - timedelta(hours=2),
            "last_job_status": last_job,
            "error_detail": error,
            "job_finished_at": NOW - timedelta(hours=1),
        }
        sp.acquire.return_value.__aenter__.return_value.fetch.side_effect = [
            [{"station_id": "ocpp-001"}],
            [],
            [conn_row],
        ]
        return sp, tp, cs

    def test_kempower_ok_when_last_job_succeeded(self, client):
        sp, tp, cs = self._pool_with_connection("succeeded")
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        kempower = next(n for n in resp.json()["nodes"] if n["id"] == "src:kempower")
        assert kempower["status"] == "ok"

    def test_kempower_warn_when_last_job_partial(self, client):
        sp, tp, cs = self._pool_with_connection("partial")
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        kempower = next(n for n in resp.json()["nodes"] if n["id"] == "src:kempower")
        assert kempower["status"] == "warn"

    def test_kempower_danger_when_last_job_failed(self, client):
        sp, tp, cs = self._pool_with_connection("failed", error="upstream API returned 503")
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        kempower = next(n for n in resp.json()["nodes"] if n["id"] == "src:kempower")
        assert kempower["status"] == "danger"
        assert "503" in kempower["description"]

    def test_kempower_paused_when_connection_paused(self, client):
        sp, tp, cs = self._pool_with_connection("succeeded", paused=True)
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        kempower = next(n for n in resp.json()["nodes"] if n["id"] == "src:kempower")
        assert kempower["status"] == "paused"

    def test_kempower_absent_when_no_connections(self, client):
        sp, tp, cs = _make_pool(conn_rows=[])
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert "src:kempower" not in [n["id"] for n in resp.json()["nodes"]]


class TestNavirecNode:
    """Navirec telematics health transitions."""

    def test_navirec_absent_when_poll_disabled(self, client):
        sp, tp, cs = _make_pool(navirec_latest=NOW - timedelta(minutes=5))
        with _patch_db(sp, tp, cs), patch.dict("os.environ", {"NAVIREC_POLL_ENABLED": "false"}):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert "src:navirec" not in [n["id"] for n in resp.json()["nodes"]]

    def test_navirec_ok_when_telemetry_fresh(self, client):
        sp, tp, cs = _make_pool(navirec_latest=NOW - timedelta(minutes=3))
        with _patch_db(sp, tp, cs), patch.dict("os.environ", {"NAVIREC_POLL_ENABLED": "true"}):
            with patch("src.api.main.datetime") as mock_dt:
                mock_dt.now.return_value = NOW
                resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        nav = next(n for n in resp.json()["nodes"] if n["id"] == "src:navirec")
        assert nav["status"] == "ok"

    def test_navirec_warn_when_telemetry_20min_old(self, client):
        sp, tp, cs = _make_pool(navirec_latest=NOW - timedelta(minutes=20))
        with _patch_db(sp, tp, cs), patch.dict("os.environ", {"NAVIREC_POLL_ENABLED": "true"}):
            with patch("src.api.main.datetime") as mock_dt:
                mock_dt.now.return_value = NOW
                resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        nav = next(n for n in resp.json()["nodes"] if n["id"] == "src:navirec")
        assert nav["status"] == "warn"
        assert "20min" in nav["description"]

    def test_navirec_danger_when_telemetry_90min_old(self, client):
        sp, tp, cs = _make_pool(navirec_latest=NOW - timedelta(minutes=90))
        with _patch_db(sp, tp, cs), patch.dict("os.environ", {"NAVIREC_POLL_ENABLED": "true"}):
            with patch("src.api.main.datetime") as mock_dt:
                mock_dt.now.return_value = NOW
                resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        nav = next(n for n in resp.json()["nodes"] if n["id"] == "src:navirec")
        assert nav["status"] == "danger"

    def test_navirec_warn_when_no_telemetry_yet(self, client):
        sp, tp, cs = _make_pool(navirec_latest=None)
        with _patch_db(sp, tp, cs), patch.dict("os.environ", {"NAVIREC_POLL_ENABLED": "true"}):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        nav = next(n for n in resp.json()["nodes"] if n["id"] == "src:navirec")
        assert nav["status"] == "warn"


class TestResponseShape:
    """Wire-format and schema invariants."""

    def test_response_uses_camel_case_field_names(self, client):
        sp, tp, cs = _make_pool()
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        body = resp.json()
        assert "asOf" in body
        assert "nodes" in body
        assert "edges" in body
        node = body["nodes"][0]
        assert "lastSyncAt" in node
        assert "id" in node
        assert "status" in node

    def test_edges_always_empty(self, client):
        sp, tp, cs = _make_pool()
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.json()["edges"] == []

    def test_ocpp_node_always_present(self, client):
        sp, tp, cs = _make_pool()
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert "src:ocpp" in [n["id"] for n in resp.json()["nodes"]]

    def test_description_absent_on_ok_nodes(self, client):
        sp, tp, cs = _make_pool()
        with _patch_db(sp, tp, cs):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        for node in resp.json()["nodes"]:
            if node["status"] == "ok":
                assert node["description"] is None, (
                    f"ok node {node['id']} should have no description"
                )

    def test_503_when_db_unavailable(self, client):
        with patch("src.api.main.db_pools", None):
            resp = client.get(f"/depots/{DEPOT_ID}/data-graph")
        assert resp.status_code == 503
