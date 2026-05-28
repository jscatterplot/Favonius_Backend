"""Unit tests for ``dispatch_get_diagnostics``.

The dispatch helper writes two rows in a single transaction:

* ``charger_log_imports`` — status ``'requested'``, token hash stored.
* ``charging_command_queue`` — ``command_type='get_diagnostics'``,
  payload carries the signed upload URL.

These tests use a fake asyncpg-like pool to capture the SQL and args,
asserting on shape rather than running real SQL — that's covered by
the integration test.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


@pytest.fixture(autouse=True)
def _upload_env(monkeypatch):
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", "dispatch-test-key-1234567890")
    monkeypatch.setenv(
        "CHARGER_LOG_UPLOAD_BASE_URL",
        "https://api.example.test/internal/charger_logs/upload",
    )


class _FakeConn:
    """Captures every execute / fetchrow / fetchval call for assertions."""

    def __init__(self):
        self.calls: list[tuple[str, str, tuple]] = []
        self.unique_violation_on_first_execute = False

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self.unique_violation_on_first_execute:
            self.unique_violation_on_first_execute = False
            import asyncpg

            # UniqueViolationError takes a positional message only; the
            # constraint metadata isn't accessible through __init__ but
            # the type-check (``isinstance(exc, UniqueViolationError)``)
            # is what the calling code uses.
            raise asyncpg.UniqueViolationError("duplicate idempotency_key")

    @asynccontextmanager
    async def transaction(self):
        yield


class _FakePool:
    """Just enough of asyncpg.Pool for the dispatch helper to call."""

    def __init__(self, conn: _FakeConn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_dispatch_writes_import_row_and_queue_row():
    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    session_id = uuid4()
    charger_id = uuid4()

    import_id = await dispatch_get_diagnostics(
        pool,
        station_id="OCPP-01",
        session_id=session_id,
        charger_id=charger_id,
        connector_id=1,
        vendor="ABB",
        idempotency_key="key-1",
    )

    assert isinstance(import_id, UUID)
    # Two execute calls inside one transaction: insert imports + queue.
    assert len(conn.calls) == 2
    insert_imports_sql = conn.calls[0][1]
    insert_queue_sql = conn.calls[1][1]
    assert "INSERT INTO charger_log_imports" in insert_imports_sql
    assert "INSERT INTO charging_command_queue" in insert_queue_sql

    # Imports row args
    imports_args = conn.calls[0][2]
    assert imports_args[0] == import_id  # id
    assert imports_args[1] == "OCPP-01"  # station_id
    assert imports_args[2] == charger_id  # charger_id
    assert imports_args[3] == 1  # connector_id
    assert imports_args[4] == session_id  # session_id
    assert imports_args[5] == "ABB"  # vendor
    # Token hash is hex-sha256 — 64 chars
    assert isinstance(imports_args[6], str) and len(imports_args[6]) == 64
    assert imports_args[7] == "key-1"  # idempotency_key

    # Queue row args
    queue_args = conn.calls[1][2]
    assert queue_args[0] == "OCPP-01"
    assert queue_args[1] == 1  # connector_id (defaults to 0 when None)
    payload = json.loads(queue_args[2])
    assert payload["location"].startswith("https://api.example.test/internal/charger_logs/upload?")
    assert payload["import_id"] == str(import_id)


@pytest.mark.asyncio
async def test_dispatch_token_in_url_matches_persisted_hash():
    """Regression for Bugbot HIGH: dispatch used to mint the token
    twice (once for the hash, once inside ``build_upload_url``). When
    ``time.time()`` straddled a second boundary the two tokens
    differed and uploads were rejected with 401. The token embedded
    in the URL must hash to the value persisted in
    ``upload_token_hash``.
    """
    import hashlib
    from urllib.parse import parse_qs, urlparse

    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    await dispatch_get_diagnostics(pool, station_id="OCPP-MATCH")

    persisted_hash = conn.calls[0][2][6]  # upload_token_hash arg
    payload = json.loads(conn.calls[1][2][2])
    location = payload["location"]
    token_in_url = parse_qs(urlparse(location).query)["token"][0]

    assert hashlib.sha256(token_in_url.encode()).hexdigest() == persisted_hash


@pytest.mark.asyncio
async def test_queue_expiry_aligned_with_token_ttl(monkeypatch):
    """Regression for Codex P2: queue row expiry used to be a fixed
    60 min, even when ``CHARGER_LOG_UPLOAD_TOKEN_TTL_S`` was shorter.
    A charger reconnecting late could then be handed a command
    whose embedded upload URL had already expired. Dispatch now caps
    the queue expiry at the configured token TTL.
    """
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_TOKEN_TTL_S", "600")  # 10 min

    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)
    await dispatch_get_diagnostics(pool, station_id="OCPP-TTL")

    # The fifth $4 arg to the queue INSERT carries the expiry interval
    # in minutes (stringified). With a 600s TTL the queue must expire
    # at most 10 min from now.
    queue_args = conn.calls[1][2]
    expires_str = queue_args[3]
    assert int(expires_str) <= 10, (
        f"expected queue expiry <= 10 min (token TTL 600s); got {expires_str}"
    )


@pytest.mark.asyncio
async def test_dispatch_forwards_optional_ocpp_params():
    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    await dispatch_get_diagnostics(
        pool,
        station_id="OCPP-02",
        retries=3,
        retry_interval=15,
    )

    payload = json.loads(conn.calls[1][2][2])
    assert payload["retries"] == 3
    assert payload["retry_interval"] == 15


@pytest.mark.asyncio
async def test_dispatch_refuses_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("CHARGER_LOG_UPLOAD_BASE_URL", raising=False)
    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    with pytest.raises(RuntimeError) as excinfo:
        await dispatch_get_diagnostics(pool, station_id="OCPP-03")
    assert "CHARGER_LOG_UPLOAD_BASE_URL" in str(excinfo.value)
    # No DB writes should have happened.
    assert conn.calls == []


@pytest.mark.asyncio
async def test_dispatch_uses_derived_base_url_when_env_unset(monkeypatch):
    """With the env var unset, a caller-derived base_url drives the URL."""
    monkeypatch.delenv("CHARGER_LOG_UPLOAD_BASE_URL", raising=False)
    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    await dispatch_get_diagnostics(
        pool,
        station_id="OCPP-DERIVE",
        base_url="https://derived.example/internal/charger_logs/upload",
    )

    payload = json.loads(conn.calls[1][2][2])
    assert payload["location"].startswith(
        "https://derived.example/internal/charger_logs/upload?"
    )


@pytest.mark.asyncio
async def test_dispatch_env_base_url_wins_over_derived(monkeypatch):
    """Explicit CHARGER_LOG_UPLOAD_BASE_URL overrides a derived base_url.

    The env var is the operator's escape hatch for split-ingress setups,
    so it must take precedence over the request-derived value.
    """
    monkeypatch.setenv(
        "CHARGER_LOG_UPLOAD_BASE_URL",
        "https://explicit.example/internal/charger_logs/upload",
    )
    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    await dispatch_get_diagnostics(
        pool,
        station_id="OCPP-OVERRIDE",
        base_url="https://derived.example/internal/charger_logs/upload",
    )

    payload = json.loads(conn.calls[1][2][2])
    assert payload["location"].startswith(
        "https://explicit.example/internal/charger_logs/upload?"
    )


@pytest.mark.asyncio
async def test_dispatch_refuses_when_neither_env_nor_derived(monkeypatch):
    """No env var and no derived base_url → RuntimeError, no DB writes."""
    monkeypatch.delenv("CHARGER_LOG_UPLOAD_BASE_URL", raising=False)
    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    pool = _FakePool(conn)

    with pytest.raises(RuntimeError) as excinfo:
        await dispatch_get_diagnostics(pool, station_id="OCPP-NONE", base_url=None)
    assert "CHARGER_LOG_UPLOAD_BASE_URL" in str(excinfo.value)
    assert conn.calls == []


@pytest.mark.asyncio
async def test_dispatch_propagates_unique_violation():
    """Idempotency-key collision must surface so the endpoint can 409."""
    import asyncpg

    from src.adapters.ocpp.dispatch import dispatch_get_diagnostics

    conn = _FakeConn()
    conn.unique_violation_on_first_execute = True
    pool = _FakePool(conn)

    with pytest.raises(asyncpg.UniqueViolationError):
        await dispatch_get_diagnostics(
            pool,
            station_id="OCPP-04",
            idempotency_key="dup-key",
        )
