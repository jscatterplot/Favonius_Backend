"""Unit tests for the charger-log orchestration in ``src.api.charger_logs``.

Focuses on ``receive_upload`` (size cap, token validation, status guards)
and the parse-then-reconcile pipeline gates. Uses a fake asyncpg
connection that records calls and returns canned rows.
"""

from __future__ import annotations

import io
import json
import tarfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.adapters.chargers.upload_token import mint_token
from src.api.charger_logs import (
    UploadRejected,
    parse_and_persist_entries,
    receive_upload,
    reconcile_and_finalize,
)


_NOW = datetime(2024, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(hours=1)

_SAMPLE_CSV = b"""timestamp,transaction_id,connector_id,soc_percent,power_w,energy_wh
2024-05-15T10:00:00Z,42,1,15.0,7400,0
2024-05-15T10:30:00Z,42,1,18.0,7400,3700
2024-05-15T11:00:00Z,42,1,21.0,0,5550
"""


def _abb_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="session_42.csv")
        info.size = len(_SAMPLE_CSV)
        tar.addfile(info, io.BytesIO(_SAMPLE_CSV))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _signing_env(monkeypatch):
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", "orch-test-key-1234567890")
    monkeypatch.setenv(
        "CHARGER_LOG_UPLOAD_BASE_URL",
        "https://api.example.test/internal/charger_logs/upload",
    )


# ---------------------------------------------------------------------------
# Fake pool plumbing — captures every query the orchestration runs
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows_by_keyword: dict[str, object]):
        self._rows = rows_by_keyword
        self.executed: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list]] = []

    def _match_row(self, sql, default=None):
        for keyword, row in self._rows.items():
            if keyword in sql:
                # Pop one-shot rows so successive identical queries
                # can be canned independently.
                if isinstance(row, list):
                    return row.pop(0) if row else default
                return row
        return default

    async def fetchrow(self, sql, *args):
        return self._match_row(sql)

    async def fetch(self, sql, *args):
        rows = self._match_row(sql) or []
        return rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    async def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))

    @asynccontextmanager
    async def transaction(self):
        yield


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


# ---------------------------------------------------------------------------
# receive_upload — token + body validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_upload_persists_body_and_flips_status():
    import_id = uuid4()
    token = mint_token(import_id)
    import hashlib

    expected_hash = hashlib.sha256(token.encode()).hexdigest()
    row = {
        "id": import_id,
        "session_id": uuid4(),
        "station_id": "OCPP-01",
        "vendor": "ABB",
        "status": "requested",
        "upload_token_hash": expected_hash,
    }
    conn = _FakeConn({"FROM charger_log_imports": row})

    body = _abb_tarball()
    result = await receive_upload(_FakePool(conn), token=token, body=body, file_name="dump.tar.gz")
    assert result.import_id == import_id
    assert result.file_size_bytes == len(body)
    # Exactly one UPDATE flipping the row to 'received'.
    assert len(conn.executed) == 1
    update_sql = conn.executed[0][0]
    assert "SET raw_payload" in update_sql
    assert "status          = 'received'" in update_sql or "status = 'received'" in update_sql


@pytest.mark.asyncio
async def test_receive_upload_rejects_oversized_body(monkeypatch):
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_MAX_BYTES", "1024")
    import_id = uuid4()
    token = mint_token(import_id)
    conn = _FakeConn({})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token=token, body=b"x" * 2048)
    assert excinfo.value.status_code == 413


@pytest.mark.asyncio
async def test_receive_upload_rejects_empty_body():
    import_id = uuid4()
    token = mint_token(import_id)
    conn = _FakeConn({})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token=token, body=b"")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_receive_upload_rejects_invalid_token():
    conn = _FakeConn({})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token="not-a-token", body=b"some payload")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_receive_upload_skips_reverify_when_decoded_passed():
    """Regression for Cursor HIGH: the upload endpoint verifies the
    token, streams the body, then calls receive_upload which used to
    verify *again*. Between the two calls the token's expiry could
    elapse, rejecting an upload the pre-check accepted. receive_upload
    now accepts a ``decoded`` ``UploadToken`` from the endpoint and
    re-uses it, so both verdicts are consistent.
    """
    import hashlib

    from src.adapters.chargers.upload_token import UploadToken

    import_id = uuid4()
    fake_token = f"{import_id}.99999999.deadbeef" + "00" * 28
    expected_hash = hashlib.sha256(fake_token.encode()).hexdigest()
    decoded = UploadToken(import_id=import_id, expiry_unix=99999999, raw=fake_token)

    row = {
        "id": import_id,
        "session_id": uuid4(),
        "station_id": "OCPP-01",
        "vendor": "ABB",
        "status": "requested",
        "upload_token_hash": expected_hash,
    }
    conn = _FakeConn({"FROM charger_log_imports": row})

    # Even with a syntactically invalid token (would fail verify_token),
    # passing decoded= bypasses the second verify and proceeds to the
    # DB write.
    result = await receive_upload(
        _FakePool(conn),
        token=fake_token,
        body=b"payload",
        decoded=decoded,
    )
    assert result.import_id == import_id


@pytest.mark.asyncio
async def test_receive_upload_does_not_leak_token_reason():
    """Regression for Cursor LOW: the rejection reason used to embed
    the underlying ``UploadTokenError`` (``malformed`` /
    ``bad signature`` / ``expired``) into the response body, which
    is a content side channel. The reason now reads simply
    ``invalid token`` regardless of which sub-check tripped.
    """
    conn = _FakeConn({})
    # Malformed (wrong segment count) — different from expired
    # versus bad-signature, yet all must produce the same client-
    # visible message.
    with pytest.raises(UploadRejected) as excinfo_malformed:
        await receive_upload(_FakePool(conn), token="too.few", body=b"payload")
    assert excinfo_malformed.value.reason == "invalid token"
    # Well-formed shape, wrong signature: same generic message.
    with pytest.raises(UploadRejected) as excinfo_badsig:
        await receive_upload(
            _FakePool(conn),
            token=f"{uuid4()}.0.deadbeef" + "00" * 28,
            body=b"payload",
        )
    assert excinfo_badsig.value.reason == "invalid token"


@pytest.mark.asyncio
async def test_receive_upload_returns_404_when_row_missing():
    import_id = uuid4()
    token = mint_token(import_id)
    conn = _FakeConn({"FROM charger_log_imports": None})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token=token, body=b"payload")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_receive_upload_rejects_already_received():
    """Re-upload of the same import is a 409 (not silently overwrite)."""
    import_id = uuid4()
    token = mint_token(import_id)
    import hashlib

    row = {
        "id": import_id,
        "session_id": uuid4(),
        "station_id": "OCPP-01",
        "vendor": "ABB",
        "status": "received",
        "upload_token_hash": hashlib.sha256(token.encode()).hexdigest(),
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token=token, body=b"payload")
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_receive_upload_rejects_token_hash_mismatch():
    """Defeats replay across imports: a token minted with a different
    import_id should not be acceptable for this import's row."""
    import_id = uuid4()
    correct_token = mint_token(import_id)
    import hashlib

    row = {
        "id": import_id,
        "session_id": uuid4(),
        "station_id": "OCPP-01",
        "vendor": "ABB",
        "status": "requested",
        # Hash recorded at dispatch time corresponds to the *other* token
        "upload_token_hash": hashlib.sha256(b"different-token").hexdigest(),
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token=correct_token, body=b"payload")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_receive_upload_maps_missing_signing_key_to_503(monkeypatch):
    """Regression for Codex P2: ``verify_token`` raises ``RuntimeError``
    when ``CHARGER_LOG_UPLOAD_SIGNING_KEY`` is unset *after* format
    checks pass. Without explicit handling the endpoint returned 500.
    ``receive_upload`` now maps it to a 503 ``UploadRejected`` so the
    operator sees a config signal instead of an opaque server error.
    """
    # Mint a well-formed token with the key set, then drop the key so
    # ``verify_token`` reaches the HMAC step and raises RuntimeError
    # (not UploadTokenError "malformed token").
    token = mint_token(uuid4())
    monkeypatch.delenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", raising=False)
    conn = _FakeConn({})
    with pytest.raises(UploadRejected) as excinfo:
        await receive_upload(_FakePool(conn), token=token, body=b"payload")
    assert excinfo.value.status_code == 503
    assert "misconfigured" in excinfo.value.reason


# ---------------------------------------------------------------------------
# parse_and_persist_entries — parser dispatch + status transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_inserts_entries_and_flips_to_parsed():
    import_id = uuid4()
    blob = _abb_tarball()
    row = {
        "id": import_id,
        "station_id": "OCPP-01",
        "charger_id": uuid4(),
        "connector_id": 1,
        "session_id": uuid4(),
        "vendor": "ABB",
        "raw_payload": blob,
        "status": "received",
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    n = await parse_and_persist_entries(_FakePool(conn), import_id=import_id)
    assert n == 3
    # One executemany insert + one UPDATE to flip status.
    assert len(conn.executemany_calls) == 1
    rows_inserted = conn.executemany_calls[0][1]
    assert len(rows_inserted) == 3
    assert any("status     = 'parsed'" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_parse_unknown_vendor_leaves_row_alone():
    """Unsupported vendor keeps the raw blob for future backfill."""
    import_id = uuid4()
    row = {
        "id": import_id,
        "station_id": "OCPP-01",
        "charger_id": uuid4(),
        "connector_id": 1,
        "session_id": uuid4(),
        "vendor": "Wallbox",
        "raw_payload": _abb_tarball(),
        "status": "received",
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    n = await parse_and_persist_entries(_FakePool(conn), import_id=import_id)
    assert n == 0
    assert conn.executemany_calls == []  # no entries inserted
    # Row was NOT flipped to 'failed' — it stays 'received' for backfill.
    assert not any("status        = 'failed'" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_parse_empty_archive_marks_parsed_with_zero_entries():
    """Tar with no recognisable session-log file → 'parsed' status, 0 rows."""
    empty_tar_buf = io.BytesIO()
    with tarfile.open(fileobj=empty_tar_buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="other_dump.csv")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"foo\n"))
    import_id = uuid4()
    row = {
        "id": import_id,
        "station_id": "OCPP-01",
        "charger_id": uuid4(),
        "connector_id": 1,
        "session_id": uuid4(),
        "vendor": "ABB",
        "raw_payload": empty_tar_buf.getvalue(),
        "status": "received",
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    n = await parse_and_persist_entries(_FakePool(conn), import_id=import_id)
    assert n == 0
    # Should have flipped to 'parsed' so reconciler can run and emit 'no_log_entries'.
    assert any("status     = 'parsed'" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_parse_idempotent_when_already_parsed():
    """Re-invoking parse on a 'parsed' row is a no-op."""
    import_id = uuid4()
    row = {
        "id": import_id,
        "station_id": "OCPP-01",
        "charger_id": uuid4(),
        "connector_id": 1,
        "session_id": uuid4(),
        "vendor": "ABB",
        "raw_payload": _abb_tarball(),
        "status": "parsed",
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    n = await parse_and_persist_entries(_FakePool(conn), import_id=import_id)
    assert n == 0
    assert conn.executemany_calls == []


# ---------------------------------------------------------------------------
# reconcile_and_finalize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_skips_when_status_not_parsed():
    import_id = uuid4()
    row = {
        "id": import_id,
        "session_id": uuid4(),
        "vendor": "ABB",
        "status": "received",  # not yet parsed
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    result = await reconcile_and_finalize(_FakePool(conn), import_id=import_id)
    assert result is None


@pytest.mark.asyncio
async def test_reconcile_skips_when_session_id_is_null():
    """Depot-wide imports (future) don't run the session reconciler."""
    import_id = uuid4()
    row = {
        "id": import_id,
        "session_id": None,
        "vendor": "ABB",
        "status": "parsed",
    }
    conn = _FakeConn({"FROM charger_log_imports": row})
    result = await reconcile_and_finalize(_FakePool(conn), import_id=import_id)
    assert result is None


@pytest.mark.asyncio
async def test_run_post_upload_pipeline_marks_failed_on_unexpected_exception():
    """Regression for Codex P2: previously a DB error mid-pipeline left
    the import in ``received`` / ``parsed`` forever because
    ``run_post_upload_pipeline`` only logged. The pipeline now flips
    the row to ``failed`` on any uncaught exception so operators see
    a terminal state.
    """
    from src.api.charger_logs import run_post_upload_pipeline

    import_id = uuid4()
    executed_sqls: list[str] = []

    class _Conn:
        async def fetchrow(self, *a, **k):
            # Boom — simulate a transient DB / network failure that
            # neither parse nor reconcile already handles.
            raise RuntimeError("connection reset")

        async def execute(self, sql, *a, **k):
            executed_sqls.append(sql)
            return "UPDATE 1"

    class _Pool:
        conn = _Conn()

        @asynccontextmanager
        async def acquire(self):
            yield self.conn

    await run_post_upload_pipeline(_Pool(), import_id=import_id)
    # The pipeline's catch-all must have flipped the row to 'failed' via
    # _mark_import_failed. Match on the canonical UPDATE shape.
    assert any(
        "SET status        = 'failed'" in sql for sql in executed_sqls
    ), f"no failure UPDATE issued; executed: {executed_sqls}"


@pytest.mark.asyncio
async def test_reconcile_runs_for_failed_imports():
    """Regression for Codex P2: a parse failure used to skip the
    reconciler, leaving the read endpoint with no source enum to
    display. ``reconcile_and_finalize`` now runs even when the import
    row is in ``status='failed'`` so a ``source='parse_failed'``
    row appears in ``session_log_reconciliations``. The import row
    itself stays in ``failed`` — only ``parsed`` advances to
    ``reconciled``.
    """
    import_id = uuid4()
    session_id = uuid4()
    # First fetchrow returns the import (status='failed'), second
    # returns the session (so reconcile_session_log can resolve it),
    # third returns the import_row inside reconcile_session_log,
    # fourth returns the charger aggregate.
    import_row_failed = {
        "id": import_id,
        "session_id": session_id,
        "vendor": "ABB",
        "status": "failed",
        "error_message": "tar corrupt",
    }
    # Sequence the two fetches against ``FROM charger_log_imports``:
    # outer reconcile_and_finalize lookup first, then the lookup
    # inside reconcile_session_log.
    conn = _FakeConn(
        {
            "FROM charger_log_imports": [import_row_failed, import_row_failed],
            "FROM charging_sessions": None,  # no session → source='no_session'
        }
    )
    result = await reconcile_and_finalize(_FakePool(conn), import_id=import_id)
    assert result is not None
    # Even with no session/no entries, the reconciler emits a row —
    # the source enum tells the UI what happened.
    assert result.source in {"no_session", "parse_failed"}
    # No status flip — 'failed' is terminal.
    assert not any(
        "SET status         = 'reconciled'" in sql for sql, _ in conn.executed
    )
