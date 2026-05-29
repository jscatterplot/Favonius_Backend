"""Integration tests for the document-fill persistence layer (migration 047).

Applies ``migrations/047_agent_document_fill.sql`` (idempotent) against the
real test database, then exercises ``src/api/agent/document_store.py`` end to
end: template storage + idempotency, session open/scope/update, and output
storage + owner-scoped download. This validates BOTH the migration DDL and the
async SQL helpers against real Postgres.

Skipped automatically when no test database is reachable (the ``pool`` fixture
in ``tests/integration/conftest.py`` resolves the URL).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from src.api.agent import document_store as ds

pytestmark = [pytest.mark.integration, pytest.mark.database]

_MIGRATION = Path(__file__).parents[1].parent / "migrations" / "047_agent_document_fill.sql"


@pytest_asyncio.fixture
async def doc_pool(pool):
    """Apply migration 047 (idempotent) and yield the TimescaleDB pool."""
    sql = _MIGRATION.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    yield pool


@pytest.mark.asyncio
async def test_template_store_load_and_idempotency(doc_pool):
    org_id, user_id = uuid4(), uuid4()
    key = f"test-idem-{uuid4()}"
    tid = await ds.store_template(
        doc_pool,
        organization_id=org_id,
        depot_id=None,
        uploaded_by=user_id,
        kind="docx",
        pdf_form_type=None,
        file_name="report.docx",
        file_size_bytes=123,
        content_sha256="abc",
        raw_payload=b"PK\x03\x04docx-bytes",
        detected_fields=[{"name": "total", "source": "jinja"}],
        idempotency_key=key,
    )
    # Same idempotency key → same row, no duplicate.
    tid2 = await ds.store_template(
        doc_pool,
        organization_id=org_id,
        depot_id=None,
        uploaded_by=user_id,
        kind="docx",
        pdf_form_type=None,
        file_name="report.docx",
        file_size_bytes=123,
        content_sha256="abc",
        raw_payload=b"PK\x03\x04docx-bytes",
        detected_fields=[{"name": "total", "source": "jinja"}],
        idempotency_key=key,
    )
    assert tid == tid2

    row = await ds.load_template(doc_pool, tid)
    assert row is not None
    assert row["kind"] == "docx"
    assert row["detected_fields"] == [{"name": "total", "source": "jinja"}]
    assert bytes(row["raw_payload"]) == b"PK\x03\x04docx-bytes"


@pytest.mark.asyncio
async def test_session_scope_and_update(doc_pool):
    org_id, owner, other = uuid4(), uuid4(), uuid4()
    tid = await ds.store_template(
        doc_pool,
        organization_id=org_id,
        depot_id=None,
        uploaded_by=owner,
        kind="docx",
        pdf_form_type=None,
        file_name="r.docx",
        file_size_bytes=1,
        content_sha256="x",
        raw_payload=b"PK\x03\x04",
        detected_fields=[],
    )
    sid = await ds.open_session(
        doc_pool, template_id=tid, organization_id=org_id, depot_id=None, user_id=owner
    )

    # Owner sees it; a different user does not; an admin does.
    assert await ds.load_session_for_user(doc_pool, sid, owner, is_admin=False) is not None
    assert await ds.load_session_for_user(doc_pool, sid, other, is_admin=False) is None
    assert await ds.load_session_for_user(doc_pool, sid, other, is_admin=True) is not None

    # Update reflects new status/draft/log.
    await ds.update_session(
        doc_pool,
        sid,
        status="awaiting_input",
        draft={"field_values": {"total": "15,000 kWh"}, "open_questions": [{"text": "which?"}]},
        message_log=[{"role": "user", "text": "fill it"}],
        latest_output_id=None,
    )
    loaded = await ds.load_session_for_user(doc_pool, sid, owner, is_admin=False)
    assert loaded["status"] == "awaiting_input"
    assert loaded["draft"]["field_values"]["total"] == "15,000 kWh"
    assert loaded["message_log"] == [{"role": "user", "text": "fill it"}]


@pytest.mark.asyncio
async def test_output_store_and_owner_scoped_download(doc_pool):
    org_id, owner, other = uuid4(), uuid4(), uuid4()
    tid = await ds.store_template(
        doc_pool,
        organization_id=org_id,
        depot_id=None,
        uploaded_by=owner,
        kind="pdf",
        pdf_form_type="acroform",
        file_name="form.pdf",
        file_size_bytes=1,
        content_sha256="x",
        raw_payload=b"%PDF-1.7",
        detected_fields=[{"name": "customer_name", "source": "acroform"}],
    )
    sid = await ds.open_session(
        doc_pool, template_id=tid, organization_id=org_id, depot_id=None, user_id=owner
    )
    oid = await ds.store_output(
        doc_pool,
        template_id=tid,
        session_id=sid,
        run_id=uuid4(),
        organization_id=org_id,
        depot_id=None,
        kind="pdf",
        output_kind="final",
        fidelity="preserved",
        file_name="form_filled.pdf",
        file_size_bytes=8,
        content_sha256="y",
        raw_payload=b"%PDF-out",
        field_values={"customer_name": "ACME"},
        replacements=[],
    )

    # Owner can download; a different user is 404 (None); admin can.
    out = await ds.load_output_for_user(doc_pool, oid, owner, is_admin=False)
    assert out is not None
    assert bytes(out["raw_payload"]) == b"%PDF-out"
    assert out["output_kind"] == "final"
    assert await ds.load_output_for_user(doc_pool, oid, other, is_admin=False) is None
    assert await ds.load_output_for_user(doc_pool, oid, other, is_admin=True) is not None
