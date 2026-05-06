"""Unit tests for RateLimitMiddleware bucket-key selection.

Covers the H5 follow-up fix: the middleware must verify JWT expiration
before trusting `sub` as the rate-limit bucket key. An expired (or
otherwise invalid) token must fall back to per-IP bucketing so an
attacker holding a stale token cannot keep draining a victim's
per-user bucket.

These tests drive ``RateLimitMiddleware.dispatch`` directly with a
stub ``call_next`` to avoid pulling in FastAPI routing, the database
pool, or the OCPP server.
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from src.security import auth as auth_mod

from src.api.main import RateLimitMiddleware
from src.security import rate_limiter as _rl_module
from src.security.rate_limiter import RateLimiter, set_rate_limiter

SECRET = "rate_limit_middleware_test_secret"


@pytest.fixture
def middleware():
    """Construct a middleware instance bound to a stub ASGI app."""
    return RateLimitMiddleware(app=MagicMock())


@pytest.fixture(autouse=True)
def _set_jwt_env():
    """Provide JWT_SECRET_KEY so the middleware can decode tokens."""
    with patch.dict(os.environ, {"JWT_SECRET_KEY": SECRET}, clear=False):
        os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
        yield


@pytest.fixture
def fresh_limiter():
    """Install a fresh in-memory RateLimiter as the module singleton."""
    fresh = RateLimiter()
    set_rate_limiter(fresh)
    try:
        yield fresh
    finally:
        # Restore the module default so other tests are unaffected.
        _rl_module._rate_limiter = None


def _make_token(
    *,
    sub: str = "user-uuid-aaa",
    expired: bool = False,
    secret: str = SECRET,
    audience: str = "authenticated",
) -> str:
    """Build an HS256 JWT with the given claims."""
    payload = {
        "sub": sub,
        "aud": audience,
        "exp": int(time.time()) + (-3600 if expired else 3600),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _make_request(
    *,
    ip: str = "1.2.3.4",
    token: str | None = None,
    path: str = "/health",
    method: str = "GET",
) -> MagicMock:
    """Build a minimal Request stub the middleware can read."""
    headers: dict[str, str] = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = ip
    request.url.path = path
    request.method = method
    request.headers = headers
    return request


def _stub_call_next() -> AsyncMock:
    """Return an AsyncMock that mimics a downstream Response with a header dict."""
    response = MagicMock()
    response.headers = {}
    response.status_code = 200
    return AsyncMock(return_value=response)


@pytest.mark.asyncio
async def test_valid_token_keys_on_user_sub(middleware, fresh_limiter):
    """A signed, unexpired token buckets the request on `user:<sub>`."""
    sub = str(uuid4())
    request = _make_request(ip="9.9.9.9", token=_make_token(sub=sub))

    await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" in fresh_limiter._api_buckets
    assert "9.9.9.9" not in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_es256_jwks_token_keys_on_user_sub_no_hs_secret(middleware, fresh_limiter):
    """ES256 + JWKS still buckets on `user:<sub>` when JWT_SECRET_KEY is unset."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    sub = str(uuid4())
    payload = {
        "sub": sub,
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    token = jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": "k1"})

    fake_client = MagicMock()
    fake_client.get_signing_key_from_jwt.return_value = _FakeSigningKey(public_key)

    with (
        patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co"}, clear=False),
        patch.object(auth_mod, "PyJWKClient", return_value=fake_client),
    ):
        for var in ("JWT_SECRET_KEY", "JWT_SECRET_KEY_PREVIOUS"):
            os.environ.pop(var, None)
        auth_mod._reset_jwks_client_for_tests()
        request = _make_request(ip="9.9.9.9", token=token)
        await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" in fresh_limiter._api_buckets
    assert "9.9.9.9" not in fresh_limiter._api_buckets
    auth_mod._reset_jwks_client_for_tests()


@pytest.mark.asyncio
async def test_expired_token_falls_back_to_ip(middleware, fresh_limiter):
    """An expired token must NOT key on `user:<sub>` — fall back to the IP."""
    sub = str(uuid4())
    request = _make_request(ip="9.9.9.9", token=_make_token(sub=sub, expired=True))

    await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" not in fresh_limiter._api_buckets
    assert "9.9.9.9" in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_missing_token_keys_on_ip(middleware, fresh_limiter):
    """Requests with no Authorization header bucket on the request IP."""
    request = _make_request(ip="9.9.9.9", token=None)

    await middleware.dispatch(request, _stub_call_next())

    assert "9.9.9.9" in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_tampered_token_keys_on_ip(middleware, fresh_limiter):
    """A token signed with a wrong key fails verification → IP fallback."""
    sub = str(uuid4())
    request = _make_request(
        ip="9.9.9.9",
        token=_make_token(sub=sub, secret="not_the_real_secret"),
    )

    await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" not in fresh_limiter._api_buckets
    assert "9.9.9.9" in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_wrong_audience_keys_on_ip(middleware, fresh_limiter):
    """A token whose `aud` is not 'authenticated' fails verification → IP."""
    sub = str(uuid4())
    request = _make_request(
        ip="9.9.9.9",
        token=_make_token(sub=sub, audience="other-service"),
    )

    await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" not in fresh_limiter._api_buckets
    assert "9.9.9.9" in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_valid_and_expired_from_same_ip_isolated(middleware, fresh_limiter):
    """Same-IP traffic stays in distinct buckets across valid vs expired tokens."""
    sub = str(uuid4())
    valid = _make_token(sub=sub)
    expired = _make_token(sub="other-user-id", expired=True)

    await middleware.dispatch(
        _make_request(ip="1.2.3.4", token=valid),
        _stub_call_next(),
    )
    await middleware.dispatch(
        _make_request(ip="1.2.3.4", token=expired),
        _stub_call_next(),
    )

    assert len(fresh_limiter._api_buckets[f"user:{sub}"]) == 1
    assert len(fresh_limiter._api_buckets["1.2.3.4"]) == 1
    # Critically: the expired-token caller did NOT consume the user bucket.
    assert "user:other-user-id" not in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_per_user_bucket_exhausts_independently(middleware, fresh_limiter):
    """Exhausting user A's bucket does not affect user B or the IP fallback."""
    sub_a = str(uuid4())
    sub_b = str(uuid4())
    token_a = _make_token(sub=sub_a)
    token_b = _make_token(sub=sub_b)

    # Drain user A's API bucket (limit = 100/min).
    for _ in range(100):
        resp = await middleware.dispatch(
            _make_request(ip="1.2.3.4", token=token_a),
            _stub_call_next(),
        )
        assert getattr(resp, "status_code", None) != 429

    # 101st request from user A should be rate-limited.
    resp = await middleware.dispatch(
        _make_request(ip="1.2.3.4", token=token_a),
        _stub_call_next(),
    )
    assert resp.status_code == 429

    # User B (different sub, same IP) is unaffected.
    resp_b = await middleware.dispatch(
        _make_request(ip="1.2.3.4", token=token_b),
        _stub_call_next(),
    )
    assert getattr(resp_b, "status_code", None) != 429
    assert len(fresh_limiter._api_buckets[f"user:{sub_b}"]) == 1

    # IP fallback bucket also untouched (no unauthenticated traffic landed yet).
    assert "1.2.3.4" not in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_expired_token_does_not_drain_user_bucket(middleware, fresh_limiter):
    """Repeated expired-token requests cannot fill the victim's per-user bucket."""
    sub = str(uuid4())
    expired = _make_token(sub=sub, expired=True)

    for _ in range(50):
        await middleware.dispatch(
            _make_request(ip="9.9.9.9", token=expired),
            _stub_call_next(),
        )

    assert f"user:{sub}" not in fresh_limiter._api_buckets
    # All 50 hits landed on the IP-keyed bucket instead.
    assert len(fresh_limiter._api_buckets["9.9.9.9"]) == 50


@pytest.mark.asyncio
async def test_healthz_bypasses_rate_limit(middleware, fresh_limiter):
    """`/healthz` is exempt and must not record any bucket entry."""
    request = _make_request(ip="9.9.9.9", token=None, path="/healthz")

    await middleware.dispatch(request, _stub_call_next())

    assert not fresh_limiter._api_buckets


# ============ Admin bulk-import write bucket ============
#
# The fleet-identity bulk-import dialog (favonius_frontend PR #62) issues
# sequential POST /admin/depots/{id}/rfid-cards calls — one per row. The
# middleware routes those to a dedicated admin-write bucket so a 200-row
# import does not consume the 100/min general API budget. These tests pin
# down the routing rules.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", f"/admin/depots/{uuid4()}/rfid-cards"),
        ("PATCH", f"/admin/depots/{uuid4()}/rfid-cards/{uuid4()}"),
        ("POST", f"/admin/depots/{uuid4()}/vehicles"),
        ("PATCH", f"/admin/depots/{uuid4()}/vehicles/{uuid4()}"),
        ("POST", f"/admin/depots/{uuid4()}/drivers"),
        ("PATCH", f"/admin/depots/{uuid4()}/drivers/{uuid4()}"),
        # Reports → Energy accounting bulk XLSX import: same per-row pattern
        # as the RFID bulk dialog, so it shares the admin-write bucket.
        ("POST", f"/admin/depots/{uuid4()}/charging-sessions/import"),
    ],
)
async def test_admin_bulk_writes_use_admin_write_bucket(
    middleware, fresh_limiter, method, path
):
    """Identity write paths land in the admin-write bucket, not the general one."""
    sub = str(uuid4())
    request = _make_request(
        ip="9.9.9.9", token=_make_token(sub=sub), path=path, method=method
    )

    await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" in fresh_limiter._admin_write_buckets
    assert f"user:{sub}" not in fresh_limiter._api_buckets


@pytest.mark.asyncio
async def test_admin_get_uses_general_api_bucket(middleware, fresh_limiter):
    """GETs under /admin/depots/.../vehicles still go to the general bucket."""
    sub = str(uuid4())
    request = _make_request(
        ip="9.9.9.9",
        token=_make_token(sub=sub),
        path=f"/admin/depots/{uuid4()}/vehicles",
        method="GET",
    )

    await middleware.dispatch(request, _stub_call_next())

    assert f"user:{sub}" in fresh_limiter._api_buckets
    assert f"user:{sub}" not in fresh_limiter._admin_write_buckets


@pytest.mark.asyncio
async def test_admin_write_bucket_rejects_when_drained(middleware):
    """A drained admin-write bucket returns 429 with RATE_LIMIT_EXCEEDED."""
    from src.security.rate_limiter import RateLimitConfig, RateLimiter

    config = RateLimitConfig(admin_write_requests_per_minute=2)
    fresh = RateLimiter(config=config)
    set_rate_limiter(fresh)
    try:
        sub = str(uuid4())
        token = _make_token(sub=sub)
        path = f"/admin/depots/{uuid4()}/rfid-cards"

        for _ in range(2):
            resp = await middleware.dispatch(
                _make_request(ip="9.9.9.9", token=token, path=path, method="POST"),
                _stub_call_next(),
            )
            assert getattr(resp, "status_code", None) != 429

        resp = await middleware.dispatch(
            _make_request(ip="9.9.9.9", token=token, path=path, method="POST"),
            _stub_call_next(),
        )
        assert resp.status_code == 429
    finally:
        _rl_module._rate_limiter = None


@pytest.mark.asyncio
async def test_admin_write_does_not_double_charge_general_bucket(middleware, fresh_limiter):
    """An admin-write request does not also count against the general API bucket.

    The middleware must pick exactly one bucket; otherwise a sequential bulk
    import would consume both and still trip the 100/min general limit.
    """
    sub = str(uuid4())
    token = _make_token(sub=sub)
    path = f"/admin/depots/{uuid4()}/rfid-cards"

    for _ in range(150):
        resp = await middleware.dispatch(
            _make_request(ip="9.9.9.9", token=token, path=path, method="POST"),
            _stub_call_next(),
        )
        assert getattr(resp, "status_code", None) != 429

    assert f"user:{sub}" not in fresh_limiter._api_buckets
    assert len(fresh_limiter._admin_write_buckets[f"user:{sub}"]) == 150
