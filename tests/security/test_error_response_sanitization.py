"""Security tests: error responses must not leak internals.

These tests assert the contract from ``CLAUDE.md`` engineering preferences
("Explicit over clever") and the ``error_codes`` design doc: every response
body produced by ``src/api/main.py`` exception handlers contains only
sanitized text. SQL fragments, table/column names, file paths, exception
class names, and other reconnaissance signals must stay in the server log.

If any of these tests start failing, treat it as a P1 — an attacker can
observe error responses to map the database schema or filesystem layout.
"""

from __future__ import annotations

import re
import sys
from typing import Iterable
from unittest.mock import MagicMock

# Stub pyomo before importing src.api.main (matches tests/unit/conftest.py).
if "pyomo" not in sys.modules:
    _pyomo_mock = MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from fastapi import APIRouter, FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.error_codes import ERROR_MESSAGES, ErrorCode  # noqa: E402
from src.api.main import DatabaseError, OptimizationError, app  # noqa: E402

# Patterns that should never appear in any error response body.
# Each pattern represents a class of leak we have seen before.
_LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SQL keyword", re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b")),
    ("table/relation reference", re.compile(r'relation\s+"[^"]+"\s+does\s+not\s+exist', re.I)),
    ("constraint name", re.compile(r'constraint\s+"[^"]+"', re.I)),
    ("filesystem path", re.compile(r"/etc/[a-zA-Z0-9_\-./]+")),
    ("postgres column ref", re.compile(r"column\s+\"[^\"]+\"\s+of\s+relation", re.I)),
    ("python traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("python exception class", re.compile(r"\b(Runtime|Type|Key|Attribute)Error\b")),
    ("asyncpg internal", re.compile(r"asyncpg\.[A-Za-z]+Error")),
]


def _assert_no_leaks(text: str, *, exempt: Iterable[str] = ()) -> None:
    for label, pattern in _LEAK_PATTERNS:
        if label in exempt:
            continue
        match = pattern.search(text)
        assert match is None, (
            f"Error response leaked {label!r}: matched {match.group(0)!r}\n"
            f"Full response: {text}"
        )


def _make_test_app() -> FastAPI:
    """Mirror ``src.api.main`` handlers and middleware on a minimal app."""
    test_app = FastAPI()
    for exc_cls, handler in app.exception_handlers.items():
        test_app.add_exception_handler(exc_cls, handler)
    from src.api.main import RequestIdMiddleware

    test_app.add_middleware(RequestIdMiddleware)

    router = APIRouter()

    @router.get("/leak/asyncpg-unique")
    async def _():
        raise asyncpg.exceptions.UniqueViolationError(
            'duplicate key value violates unique constraint "users_email_key"'
        )

    @router.get("/leak/asyncpg-relation")
    async def _():
        raise asyncpg.exceptions.UndefinedTableError('relation "stations" does not exist')

    @router.get("/leak/db-with-sql")
    async def _():
        # Real-world pattern: callers wrap asyncpg errors in DatabaseError.
        raise DatabaseError("Database error: SELECT * FROM organization_users WHERE id = $1 failed")

    @router.get("/leak/runtime-fs")
    async def _():
        raise RuntimeError("Could not load /etc/postgres/secret.conf at line 42")

    @router.get("/leak/traceback")
    async def _():
        try:
            raise KeyError("nested")
        except KeyError as e:
            raise RuntimeError("upstream failed") from e

    @router.get("/leak/optimizer")
    async def _():
        raise OptimizationError(
            "constraint violation in vehicle_charging[bus_42, t=12]: " "see /var/log/gurobi.log"
        )

    @router.get("/leak/http-500-message")
    async def _():
        # The HTTPException handler must also resist legacy callers that pass
        # f"...: {str(e)}" as detail.
        raise HTTPException(
            status_code=500,
            detail='Failed to get depot state: relation "depots" does not exist',
        )

    test_app.include_router(router)
    return test_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_test_app(), raise_server_exceptions=False)


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_asyncpg_unique_violation_no_leak(client: TestClient) -> None:
    response = client.get("/leak/asyncpg-unique")
    assert response.status_code == 503
    _assert_no_leaks(response.text)
    assert response.json()["error_code"] == ErrorCode.DATABASE_ERROR.value


def test_asyncpg_undefined_table_no_leak(client: TestClient) -> None:
    response = client.get("/leak/asyncpg-relation")
    assert response.status_code == 503
    _assert_no_leaks(response.text)


def test_database_error_with_sql_no_leak(client: TestClient) -> None:
    response = client.get("/leak/db-with-sql")
    assert response.status_code == 503
    _assert_no_leaks(response.text)
    assert response.json()["detail"] == ERROR_MESSAGES[ErrorCode.DATABASE_ERROR]


def test_runtime_filesystem_path_no_leak(client: TestClient) -> None:
    response = client.get("/leak/runtime-fs")
    assert response.status_code == 500
    _assert_no_leaks(response.text)
    assert response.json()["error_code"] == ErrorCode.INTERNAL_ERROR.value


def test_chained_exception_no_traceback_leak(client: TestClient) -> None:
    response = client.get("/leak/traceback")
    assert response.status_code == 500
    _assert_no_leaks(response.text)


def test_optimizer_error_no_constraint_leak(client: TestClient) -> None:
    response = client.get("/leak/optimizer")
    assert response.status_code == 500
    _assert_no_leaks(response.text)
    body = response.json()
    assert body["error_code"] == ErrorCode.OPTIMIZATION_ERROR.value
    # Specifically, the vehicle id and log path must be gone.
    assert "bus_42" not in response.text
    assert "/var/log" not in response.text


def test_http_500_with_leaky_detail_does_not_propagate(client: TestClient) -> None:
    """String ``detail`` on 5xx must be replaced with the sanitized message."""
    response = client.get("/leak/http-500-message")
    assert response.status_code == 500
    body = response.json()
    assert body["error_code"] == ErrorCode.INTERNAL_ERROR.value
    assert body["detail"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
    assert "relation" not in response.text
    assert "depots" not in response.text


def test_no_legacy_str_exc_in_500_raises() -> None:
    """Production guard: ``src/api/main.py`` must not raise
    ``HTTPException(status_code=500, detail=f"...{str(e)}...")`` anywhere.

    These leaky patterns were the original motivation for this changeset.
    Any new occurrence must instead raise a typed exception (DatabaseError,
    OptimizationError, etc.) or pass a structured ``detail`` dict so the
    global handler can sanitize.
    """
    main_path = "src/api/main.py"
    with open(main_path, "r", encoding="utf-8") as f:
        source = f.read()

    forbidden = [
        re.compile(
            r'raise HTTPException\(\s*status_code\s*=\s*500\s*,\s*detail\s*=\s*f"[^"]*\{(?:str\()?e\)?\}',
            re.MULTILINE,
        ),
        re.compile(
            r'raise HTTPException\(\s*status_code\s*=\s*500\s*,\s*detail\s*=\s*f"[^"]*\{(?:str\()?exc\)?\}',
            re.MULTILINE,
        ),
    ]
    for pattern in forbidden:
        match = pattern.search(source)
        assert match is None, (
            f"Found legacy leaky 500-raise in {main_path}: {match.group(0)!r}.\n"
            "Replace with `raise DatabaseError() from e` or "
            'HTTPException(status_code=500, detail={"error_code": "INTERNAL_ERROR", "detail": "..."})'
        )
