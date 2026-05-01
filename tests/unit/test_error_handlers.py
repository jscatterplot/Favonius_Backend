"""Unit tests for global exception handlers in ``src.api.main``.

Validates:
* every handler returns the canonical ``ErrorResponse`` envelope
  (``detail`` / ``error_code`` / ``timestamp`` / ``request_id``)
* status codes match :func:`src.api.error_codes.http_status_for`
* the catch-all handler emits ``INTERNAL_ERROR`` and never leaks ``str(exc)``
* DB errors do not leak SQL fragments, table names, or column names
* validation errors expose ``field_errors`` so the frontend can highlight
  individual inputs
* the ``X-Request-ID`` header is echoed and matches the body's ``request_id``
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import patch

import asyncpg
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api.error_codes import ERROR_MESSAGES, ErrorCode, http_status_for
from src.api.main import (
    DatabaseError,
    DepotNotFoundError,
    OptimizationError,
    app,
)
from src.core.optimizer.exceptions import (
    InfeasibleModelError,
    SolverTimeoutError,
)
from src.db.exceptions import IdempotencyKeyReusedError


# ─── Test app ──────────────────────────────────────────────────────────────
#
# Rather than spinning the full app (which requires DB pools, JWT auth, geo
# blocks, etc.), copy the registered exception handlers and the request-id
# middleware onto a tiny FastAPI app with a handful of failure-injecting
# endpoints. Same handlers, isolated surface.


class _Body(BaseModel):
    name: str
    qty: int


def _make_app() -> FastAPI:
    test_app = FastAPI()
    # Re-register every exception handler attached to the production app
    for exc_cls, handler in app.exception_handlers.items():
        test_app.add_exception_handler(exc_cls, handler)
    # Re-attach the request-id middleware so request.state.request_id is set
    from src.api.main import RequestIdMiddleware

    test_app.add_middleware(RequestIdMiddleware)

    router = APIRouter()

    @router.get("/boom/value")
    async def value_error_endpoint() -> dict:
        raise ValueError("Something is invalid")

    @router.get("/boom/depot-not-found")
    async def depot_not_found_endpoint() -> dict:
        raise DepotNotFoundError("Depot abc not found")

    @router.get("/boom/db")
    async def database_error_endpoint() -> dict:
        raise DatabaseError(
            'Database error: relation "users" does not exist; '
            "INSERT INTO secret_table VALUES (...)"
        )

    @router.get("/boom/idempotency")
    async def idempotency_endpoint() -> dict:
        raise IdempotencyKeyReusedError()

    @router.get("/boom/asyncpg")
    async def asyncpg_endpoint() -> dict:
        # Real asyncpg.PostgresError subclass with leaky message
        raise asyncpg.exceptions.UniqueViolationError(
            'duplicate key value violates unique constraint "users_pkey" '
            "DETAIL: Key (id)=(1) already exists in table public.users"
        )

    @router.get("/boom/optimizer-infeasible")
    async def opt_infeasible() -> dict:
        raise InfeasibleModelError("vehicle bus_42 cannot reach 99% by departure")

    @router.get("/boom/optimizer-timeout")
    async def opt_timeout() -> dict:
        raise SolverTimeoutError(60.0)

    @router.get("/boom/optimizer-generic")
    async def opt_generic() -> dict:
        raise OptimizationError("internal solver crash with stack trace")

    @router.get("/boom/unhandled")
    async def unhandled_endpoint() -> dict:
        raise RuntimeError(
            "/etc/secret/key path leak; sql: SELECT * FROM private_table"
        )

    @router.get("/boom/http-403")
    async def http_403() -> dict:
        raise HTTPException(status_code=403, detail="Access denied")

    @router.get("/boom/http-404")
    async def http_404() -> dict:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    @router.get("/boom/http-no-detail")
    async def http_no_detail() -> dict:
        raise HTTPException(status_code=500)

    @router.get("/boom/http-structured")
    async def http_structured() -> dict:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
                "detail": "duplicate request",
                "field_errors": {"key": ["already used"]},
            },
        )

    @router.post("/boom/validation")
    async def validation_endpoint(body: _Body) -> dict:
        return {"ok": True}

    test_app.include_router(router)
    return test_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=False)


# ─── Envelope shape ────────────────────────────────────────────────────────


def _assert_envelope(body: dict, *, error_code: ErrorCode) -> None:
    """All error responses share this minimum shape."""
    assert body["error_code"] == error_code.value
    assert isinstance(body["detail"], str) and body["detail"]
    assert isinstance(body["timestamp"], str) and body["timestamp"]
    assert isinstance(body["request_id"], str) and body["request_id"]


# ─── Catch-all unhandled exception ─────────────────────────────────────────


def test_unhandled_exception_returns_internal_error(client: TestClient) -> None:
    response = client.get("/boom/unhandled")
    assert response.status_code == 500
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.INTERNAL_ERROR)
    assert body["detail"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]


def test_unhandled_exception_does_not_leak_str_exc(client: TestClient) -> None:
    response = client.get("/boom/unhandled")
    body = response.json()
    serialized = response.text
    # None of the raw exception fragments should appear in the response
    assert "/etc/secret/key" not in serialized
    assert "SELECT" not in serialized
    assert "private_table" not in serialized
    assert "RuntimeError" not in serialized
    # The detail is the canonical mapping, not str(exc)
    assert body["detail"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]


def test_unhandled_exception_logs_full_traceback(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="src.api.main"):
        client.get("/boom/unhandled")
    # Logger captured the exception type + traceback
    assert any(
        "Unhandled exception" in r.getMessage() and "RuntimeError" in r.getMessage()
        for r in caplog.records
    )
    # And exc_info captured the original message (operators can debug)
    assert any(r.exc_info is not None for r in caplog.records if "Unhandled" in r.getMessage())


# ─── DB / asyncpg sanitization ─────────────────────────────────────────────


def test_database_error_returns_database_error_code(client: TestClient) -> None:
    response = client.get("/boom/db")
    assert response.status_code == http_status_for(ErrorCode.DATABASE_ERROR)
    _assert_envelope(response.json(), error_code=ErrorCode.DATABASE_ERROR)


def test_database_error_does_not_leak_sql_or_table_names(client: TestClient) -> None:
    response = client.get("/boom/db")
    serialized = response.text
    body = response.json()
    # The constructor was called with a leaky message but the response is sanitized
    assert "users" not in serialized
    assert "secret_table" not in serialized
    assert "INSERT" not in serialized
    assert "relation" not in serialized
    assert body["detail"] == ERROR_MESSAGES[ErrorCode.DATABASE_ERROR]


def test_asyncpg_postgres_error_sanitized(client: TestClient) -> None:
    response = client.get("/boom/asyncpg")
    body = response.json()
    serialized = response.text
    assert response.status_code == http_status_for(ErrorCode.DATABASE_ERROR)
    _assert_envelope(body, error_code=ErrorCode.DATABASE_ERROR)
    # asyncpg's message names tables, columns, constraints — none must leak
    assert "users_pkey" not in serialized
    assert "duplicate key" not in serialized
    assert "public.users" not in serialized
    assert "DETAIL" not in serialized


def test_idempotency_key_reused_returns_409(client: TestClient) -> None:
    response = client.get("/boom/idempotency")
    assert response.status_code == 409
    _assert_envelope(response.json(), error_code=ErrorCode.IDEMPOTENCY_KEY_REUSED)


# ─── Optimizer error mapping ───────────────────────────────────────────────


def test_optimizer_infeasible_returns_422(client: TestClient) -> None:
    response = client.get("/boom/optimizer-infeasible")
    assert response.status_code == 422
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.OPTIMIZER_INFEASIBLE)
    # Must not echo the vehicle id from the original message
    assert "bus_42" not in response.text


def test_optimizer_timeout_returns_504(client: TestClient) -> None:
    response = client.get("/boom/optimizer-timeout")
    assert response.status_code == 504
    _assert_envelope(response.json(), error_code=ErrorCode.OPTIMIZER_TIMEOUT)


def test_optimizer_generic_returns_500(client: TestClient) -> None:
    response = client.get("/boom/optimizer-generic")
    assert response.status_code == 500
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.OPTIMIZATION_ERROR)
    assert "stack trace" not in response.text


# ─── ValueError / DepotNotFoundError ───────────────────────────────────────


def test_value_error_returns_400(client: TestClient) -> None:
    response = client.get("/boom/value")
    assert response.status_code == 400
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.INVALID_INPUT)
    # ValueError text is operator-controlled and safe to forward
    assert body["detail"] == "Something is invalid"


def test_depot_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/boom/depot-not-found")
    assert response.status_code == 404
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.DEPOT_NOT_FOUND)


# ─── HTTPException handler ─────────────────────────────────────────────────


def test_http_403_envelope(client: TestClient) -> None:
    response = client.get("/boom/http-403")
    assert response.status_code == 403
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.FORBIDDEN)
    assert body["detail"] == "Access denied"


def test_http_404_preserves_call_site_detail(client: TestClient) -> None:
    response = client.get("/boom/http-404")
    body = response.json()
    assert response.status_code == 404
    assert body["detail"] == "Vehicle not found"
    assert body["error_code"] == ErrorCode.NOT_FOUND.value


def test_http_500_no_detail_uses_generic_message(client: TestClient) -> None:
    response = client.get("/boom/http-no-detail")
    body = response.json()
    assert response.status_code == 500
    _assert_envelope(body, error_code=ErrorCode.INTERNAL_ERROR)


def test_http_structured_detail_preserved_verbatim(client: TestClient) -> None:
    """Backward compat: dict-form ``detail`` is forwarded verbatim.

    Many call sites in main.py raise
    ``HTTPException(detail={"error_code": "...", "vehicle_ids": [...]})``.
    Existing clients read ``response.json()["detail"]["error_code"]``, so the
    dict must reach them untouched. The standard envelope fields are added
    alongside, never instead.
    """
    response = client.get("/boom/http-structured")
    body = response.json()
    assert response.status_code == 409
    # Structured detail preserved as a dict
    assert isinstance(body["detail"], dict)
    assert body["detail"]["error_code"] == ErrorCode.IDEMPOTENCY_KEY_REUSED.value
    assert body["detail"]["detail"] == "duplicate request"
    assert body["detail"]["field_errors"] == {"key": ["already used"]}
    # Top-level envelope still present
    assert body["error_code"] == ErrorCode.IDEMPOTENCY_KEY_REUSED.value
    assert isinstance(body["request_id"], str) and body["request_id"]


# ─── Validation error ──────────────────────────────────────────────────────


def test_validation_error_returns_field_errors(client: TestClient) -> None:
    response = client.post("/boom/validation", json={"name": 123})
    assert response.status_code == 400
    body = response.json()
    _assert_envelope(body, error_code=ErrorCode.VALIDATION_ERROR)
    # Field errors are keyed by request path, sanitized of internal context
    assert "field_errors" in body
    assert isinstance(body["field_errors"], dict)
    # qty was missing entirely
    assert any("qty" in path for path in body["field_errors"].keys())


# ─── Request-ID correlation ────────────────────────────────────────────────


def test_request_id_is_returned_as_header_and_body(client: TestClient) -> None:
    response = client.get("/boom/db")
    assert "x-request-id" in {k.lower() for k in response.headers}
    body_id = response.json()["request_id"]
    assert response.headers["X-Request-ID"] == body_id


def test_inbound_request_id_is_preserved(client: TestClient) -> None:
    incoming = "test-correlation-" + uuid.uuid4().hex
    response = client.get("/boom/db", headers={"X-Request-ID": incoming})
    assert response.headers["X-Request-ID"] == incoming
    assert response.json()["request_id"] == incoming


def test_request_id_is_uuid_when_not_provided(client: TestClient) -> None:
    response = client.get("/boom/db")
    rid = response.json()["request_id"]
    # Must be parseable as a UUID since the middleware generates uuid4
    uuid.UUID(rid)


# ─── Real-app endpoint sweep: legacy `f"Failed to ...: {str(e)}"` is gone ─


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/depots/{}/state"),
        ("get", "/depots/{}/schedule"),
        ("get", "/depots/{}/alerts"),
    ],
)
def test_legacy_500_endpoints_no_longer_leak_str_exc(method, path) -> None:
    """Endpoints previously raised ``HTTPException(detail=f"Failed to ...: {e}")``.

    We monkeypatch the underlying handler to raise a known leaky exception
    and verify that the response now goes through the global handler with
    the sanitized envelope.
    """
    from src.api import main as main_mod
    from src.security.tenant_mirror import ensure_tenant_mirrored

    # Bypass auth + JWT
    user = {"sub": "test", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = main_mod.app.dependency_overrides.get(ensure_tenant_mirrored)
    main_mod.app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    # Bypass depot access check
    from src.security import auth as auth_mod

    orig_verify = auth_mod.verify_depot_access

    async def _allow(*_args, **_kwargs):
        return None

    auth_mod.verify_depot_access = _allow

    try:
        depot_id = str(uuid.uuid4())
        url = path.format(depot_id)

        # Force every endpoint to raise something exotic with leaky text
        leaky = "/etc/passwd; SELECT * FROM secret_table"

        def _raise(*_a, **_kw):
            raise RuntimeError(leaky)

        with patch.object(main_mod, "_get_depot_config", side_effect=_raise), patch.object(
            main_mod, "db_pools", main_mod.db_pools
        ):
            test_client = TestClient(main_mod.app, raise_server_exceptions=False)
            response = getattr(test_client, method)(url)
            # Either the global Exception handler ran or a leaf handler did,
            # but in NO case may str(e) appear in the body.
            assert leaky not in response.text
            assert "/etc/passwd" not in response.text
            assert "secret_table" not in response.text
            body = response.json()
            assert "error_code" in body
            assert "request_id" in body
    finally:
        auth_mod.verify_depot_access = orig_verify
        if prev is not None:
            main_mod.app.dependency_overrides[ensure_tenant_mirrored] = prev
        else:
            main_mod.app.dependency_overrides.pop(ensure_tenant_mirrored, None)
