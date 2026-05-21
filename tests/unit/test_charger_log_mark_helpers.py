"""Tests for ``ChargingCommandQueueConsumer._mark_import_uploading/_mark_import_failed``.

The OCPP command queue stores ``import_id`` as a string in JSONB. The
helpers used to bind that string straight to a UUID-typed column, which
asyncpg silently rejected, leaving the row stuck in ``requested``. The
helpers now coerce via :func:`_coerce_uuid` before calling the DB so
the UPDATE actually runs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from src.websocket_handler.charging_profile_manager import (
    ChargingCommandQueueConsumer,
    _coerce_uuid,
)


class _FakeConn:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _FakeTimescaleClient:
    def __init__(self):
        self.pg_pool = _FakePool()


class TestCoerceUuid:
    def test_passes_through_uuid_instances(self):
        u = uuid4()
        assert _coerce_uuid(u) is u

    def test_parses_string(self):
        u = uuid4()
        assert _coerce_uuid(str(u)) == u

    def test_returns_none_for_garbage(self):
        assert _coerce_uuid("not-a-uuid") is None
        assert _coerce_uuid(None) is None
        assert _coerce_uuid("") is None
        assert _coerce_uuid(123) is None


class TestMarkImportHelpers:
    """Bugbot HIGH regression: payload import_id arrives as a string."""

    @pytest.mark.asyncio
    async def test_mark_uploading_binds_uuid_not_string(self):
        client = _FakeTimescaleClient()
        consumer = ChargingCommandQueueConsumer(
            client, cp_lookup=lambda _cp: None
        )
        import_id = uuid4()
        payload = {"import_id": str(import_id), "location": "https://x"}
        await consumer._mark_import_uploading(payload, "diagnostics.tar.gz")
        assert len(client.pg_pool.conn.execute_calls) == 1
        _, args = client.pg_pool.conn.execute_calls[0]
        # First arg to the UPDATE must be a UUID instance — binding a
        # str to a uuid column is what triggered the silent failure.
        assert isinstance(args[0], UUID)
        assert args[0] == import_id

    @pytest.mark.asyncio
    async def test_mark_failed_binds_uuid_not_string(self):
        client = _FakeTimescaleClient()
        consumer = ChargingCommandQueueConsumer(
            client, cp_lookup=lambda _cp: None
        )
        import_id = uuid4()
        payload = {"import_id": str(import_id)}
        await consumer._mark_import_failed(payload, "boom")
        assert len(client.pg_pool.conn.execute_calls) == 1
        _, args = client.pg_pool.conn.execute_calls[0]
        assert isinstance(args[0], UUID)
        assert args[0] == import_id

    @pytest.mark.asyncio
    async def test_mark_uploading_skips_when_import_id_malformed(self):
        """A garbage import_id mustn't raise — the queue row's OCPP
        ack already succeeded and any DB write here is best-effort.
        """
        client = _FakeTimescaleClient()
        consumer = ChargingCommandQueueConsumer(
            client, cp_lookup=lambda _cp: None
        )
        await consumer._mark_import_uploading(
            {"import_id": "not-a-uuid"}, "x.tar.gz"
        )
        assert client.pg_pool.conn.execute_calls == []

    @pytest.mark.asyncio
    async def test_mark_failed_skips_when_import_id_missing(self):
        client = _FakeTimescaleClient()
        consumer = ChargingCommandQueueConsumer(
            client, cp_lookup=lambda _cp: None
        )
        await consumer._mark_import_failed({}, "boom")
        assert client.pg_pool.conn.execute_calls == []
