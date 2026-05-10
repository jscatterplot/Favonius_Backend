"""Unit tests for ``ConfigValidator._validate_timescale`` diagnostics.

Focus is on the message we emit when TimescaleDB rejects credentials. The
operator must see (a) which env var fed the credentials, (b) a redacted
host/port pointing at the right service, and (c) an actionable hint
("rotate the credential at the upstream provider"). No plaintext credentials
should ever appear in the validation detail.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest

from src.websocket_handler.config_validator import ConfigValidator


def _make_config(*, environment: str = "production") -> SimpleNamespace:
    """Build the smallest config shape that ``_validate_timescale`` needs.

    The validator only touches ``config.timescale.*`` and ``config.environment``
    in the path under test, so a SimpleNamespace is sufficient and avoids the
    full Pydantic ``Config.from_env`` boot path.
    """
    timescale = SimpleNamespace(
        host="abc123.tsdb.cloud.timescale.com",
        port=31413,
        database="tsdb",
        user="tsdbadmin",
        password="rotated_secret",
        sslmode="require",
    )
    return SimpleNamespace(timescale=timescale, environment=environment, secrets_manager=None)


@pytest.mark.asyncio
async def test_invalid_password_error_yields_actionable_message_for_service_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``TIMESCALE_SERVICE_URL`` is set, name it as the rotate target."""
    monkeypatch.setenv(
        "TIMESCALE_SERVICE_URL",
        "postgresql://tsdbadmin:rotated_secret@abc123.tsdb.cloud.timescale.com:31413/tsdb?sslmode=require",
    )
    monkeypatch.delenv("PGPASSWORD", raising=False)

    validator = ConfigValidator(_make_config())  # type: ignore[arg-type]

    async def _fake_connect(**_kwargs):
        raise asyncpg.exceptions.InvalidPasswordError(
            'password authentication failed for user "tsdbadmin"'
        )

    with patch("src.websocket_handler.config_validator.asyncpg.connect", _fake_connect):
        ok = await validator._validate_timescale()

    assert ok is False
    detail = validator.validation_details["timescale"]
    assert "Auth failure for TIMESCALE_SERVICE_URL" in detail
    assert "Rotate" in detail or "rotate" in detail
    # Must not leak password or full host/user
    assert "rotated_secret" not in detail
    assert "tsdbadmin" not in detail
    assert "abc123" not in detail
    # Should expose redacted host suffix and port for triage
    assert "port=31413" in detail
    assert "*****" in detail


@pytest.mark.asyncio
async def test_invalid_password_error_names_pgpassword_when_service_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discrete-PG path: surface ``PGPASSWORD`` as the rotate target."""
    monkeypatch.delenv("TIMESCALE_SERVICE_URL", raising=False)
    monkeypatch.setenv("PGPASSWORD", "rotated_secret")

    validator = ConfigValidator(_make_config())  # type: ignore[arg-type]

    async def _fake_connect(**_kwargs):
        raise asyncpg.exceptions.InvalidPasswordError(
            'password authentication failed for user "tsdbadmin"'
        )

    with patch("src.websocket_handler.config_validator.asyncpg.connect", _fake_connect):
        ok = await validator._validate_timescale()

    assert ok is False
    detail = validator.validation_details["timescale"]
    assert "Auth failure for PGPASSWORD" in detail
    assert "rotated_secret" not in detail


@pytest.mark.asyncio
async def test_invalid_password_error_prefers_pgpassword_when_both_sources_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both are set, discrete ``PGPASSWORD`` is the effective override."""
    monkeypatch.setenv(
        "TIMESCALE_SERVICE_URL",
        "postgresql://tsdbadmin:stale_pw@abc123.tsdb.cloud.timescale.com:31413/tsdb?sslmode=require",
    )
    monkeypatch.setenv("PGPASSWORD", "rotated_secret")

    validator = ConfigValidator(_make_config())  # type: ignore[arg-type]

    async def _fake_connect(**_kwargs):
        raise asyncpg.exceptions.InvalidPasswordError(
            'password authentication failed for user "tsdbadmin"'
        )

    with patch("src.websocket_handler.config_validator.asyncpg.connect", _fake_connect):
        ok = await validator._validate_timescale()

    assert ok is False
    detail = validator.validation_details["timescale"]
    assert "Auth failure for PGPASSWORD" in detail
    assert "Auth failure for TIMESCALE_SERVICE_URL" not in detail


@pytest.mark.asyncio
async def test_invalid_password_error_prefers_pgpassword_from_secrets_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes/file-secret PGPASSWORD should outrank service URL env var."""
    monkeypatch.setenv(
        "TIMESCALE_SERVICE_URL",
        "postgresql://tsdbadmin:stale_pw@abc123.tsdb.cloud.timescale.com:31413/tsdb?sslmode=require",
    )
    monkeypatch.delenv("PGPASSWORD", raising=False)

    class _SecretManager:
        def get_secret(self, key: str):
            if key == "PGPASSWORD":
                return "mounted_secret"
            return None

    config = _make_config()  # type: ignore[arg-type]
    config.secrets_manager = _SecretManager()
    validator = ConfigValidator(config)  # type: ignore[arg-type]

    async def _fake_connect(**_kwargs):
        raise asyncpg.exceptions.InvalidPasswordError(
            'password authentication failed for user "tsdbadmin"'
        )

    with patch("src.websocket_handler.config_validator.asyncpg.connect", _fake_connect):
        ok = await validator._validate_timescale()

    assert ok is False
    detail = validator.validation_details["timescale"]
    assert "Auth failure for PGPASSWORD" in detail
    assert "Auth failure for TIMESCALE_SERVICE_URL" not in detail


@pytest.mark.asyncio
async def test_generic_connection_failure_keeps_redaction_and_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-auth errors still get the redacted target and the existing hint."""
    monkeypatch.setenv(
        "TIMESCALE_SERVICE_URL",
        "postgresql://tsdbadmin:rotated_secret@abc123.tsdb.cloud.timescale.com:31413/tsdb",
    )

    validator = ConfigValidator(_make_config())  # type: ignore[arg-type]

    async def _fake_connect(**_kwargs):
        raise OSError("Connect call failed (Errno 111) Connection refused")

    with patch("src.websocket_handler.config_validator.asyncpg.connect", _fake_connect):
        ok = await validator._validate_timescale()

    assert ok is False
    detail = validator.validation_details["timescale"]
    assert "Connection failed for TIMESCALE_SERVICE_URL" in detail
    assert "verify PGHOST/PGPORT are reachable from Railway" in detail
    assert "rotated_secret" not in detail
    assert "tsdbadmin" not in detail
