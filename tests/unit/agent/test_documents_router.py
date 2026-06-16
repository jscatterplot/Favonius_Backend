"""HTTP-layer tests for the document-fill router (no DB).

Drives the router through FastAPI's TestClient with the auth + DB seams
overridden, so multipart parsing, status codes, and response shapes are
exercised without a database.
"""

from __future__ import annotations

import io
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.agent.document_store as ds
import src.api.agent.documents_router as dr_router
from src.api.agent.auth_context import AuthContext
from src.api.agent.documents_router import get_static_pool, get_ts_pool, router
from src.security.auth import verify_token


def _docx_bytes() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Report for {{ customer_name }}.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)

    user_id = uuid4()
    payload = {"sub": str(user_id), "app_metadata": {"favonius_role": "customer_admin"}}

    app.dependency_overrides[verify_token] = lambda: payload
    app.dependency_overrides[get_static_pool] = lambda: object()
    app.dependency_overrides[get_ts_pool] = lambda: object()

    async def _auth(_payload, _pool):
        return AuthContext(
            user_id=user_id,
            organization_id=uuid4(),
            role="customer_admin",
            visible_depot_ids=[uuid4()],
        )

    monkeypatch.setattr(dr_router, "build_auth_context", _auth)

    recorded = {}

    async def _store_template(ts_pool, **kwargs):
        recorded["template"] = kwargs
        return uuid4()

    async def _open_session(ts_pool, **kwargs):
        recorded["session"] = kwargs
        return uuid4()

    monkeypatch.setattr(ds, "store_template", _store_template)
    monkeypatch.setattr(ds, "open_session", _open_session)

    tc = TestClient(app)
    tc.recorded = recorded  # type: ignore[attr-defined]
    tc.user_id = user_id  # type: ignore[attr-defined]
    return tc


def test_upload_docx_returns_201_with_session_and_fields(client):
    resp = client.post(
        "/agent/documents",
        files={
            "file": (
                "report.docx",
                _docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "docx"
    assert body["session_id"]
    assert body["document_id"]
    assert {"name": "customer_name", "source": "jinja"} in body["detected_fields"]
    # The template was opened into a session for the calling user.
    assert client.recorded["session"]["user_id"] == client.user_id


def test_upload_unsupported_type_returns_415(client):
    resp = client.post(
        "/agent/documents",
        files={"file": ("notes.txt", b"just plain text, not a document", "text/plain")},
    )
    assert resp.status_code == 415


def test_upload_empty_file_returns_400(client):
    resp = client.post(
        "/agent/documents",
        files={"file": ("empty.docx", b"", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_download_unknown_output_returns_404(client, monkeypatch):
    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ds, "load_output_for_user", _none)
    resp = client.get(f"/agent/documents/{uuid4()}/download")
    assert resp.status_code == 404


def test_download_returns_file_with_attachment_headers(client, monkeypatch):
    async def _load(ts_pool, output_id, user_id, *, is_admin):
        return {
            "id": output_id,
            "kind": "docx",
            "output_kind": "final",
            "fidelity": "preserved",
            "file_name": "report_filled.docx",
            "raw_payload": _docx_bytes(),
            "session_user_id": user_id,
        }

    monkeypatch.setattr(ds, "load_output_for_user", _load)
    resp = client.get(f"/agent/documents/{uuid4()}/download")
    assert resp.status_code == 200
    assert (
        resp.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "attachment" in resp.headers["content-disposition"]
    assert "report_filled.docx" in resp.headers["content-disposition"]


def test_download_non_ascii_filename_does_not_500(client, monkeypatch):
    # Regression: a non-Latin-1 filename (common in this Lithuanian deployment)
    # previously crashed Response construction (latin-1 header encoding) → 500.
    async def _load(ts_pool, output_id, user_id, *, is_admin):
        return {
            "id": output_id,
            "kind": "docx",
            "output_kind": "final",
            "fidelity": "preserved",
            "file_name": "ataskaita_Šiaurė.docx",
            "raw_payload": _docx_bytes(),
            "session_user_id": user_id,
        }

    monkeypatch.setattr(ds, "load_output_for_user", _load)
    resp = client.get(f"/agent/documents/{uuid4()}/download")
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd  # the real (encoded) name is preserved
    cd.encode("latin-1")  # header value must be latin-1 safe (no crash)


def test_content_disposition_sanitizes_quotes_and_controls():
    from src.api.agent.documents_router import _content_disposition

    cd = _content_disposition('a"b\r\nc.docx')
    cd.encode("latin-1")  # no raw control chars / crash
    assert "\r" not in cd and "\n" not in cd
    # the quoted ascii fallback must not contain a raw double-quote that breaks out
    fallback = cd.split("filename=", 1)[1].split(";", 1)[0]
    assert fallback.count('"') == 2  # exactly the surrounding quotes
