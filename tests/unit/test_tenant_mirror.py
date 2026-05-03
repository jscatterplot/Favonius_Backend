"""Unit tests for JIT tenant mirroring from JWT into static DB."""

from __future__ import annotations

import pytest

from src.security import tenant_mirror as tm


class _FakeTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


class _FakeConn:
    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, q, *args):
        self.executes.append((q, tuple(args)))


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *args):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _FakeAcquire(self.conn)


class _BrokenAcquire:
    async def __aenter__(self):
        raise RuntimeError("db down")

    async def __aexit__(self, *args):
        return False


class _BrokenPool:
    def acquire(self):
        return _BrokenAcquire()


@pytest.fixture(autouse=True)
def _clear_mirror_cache():
    tm.clear_tenant_mirror_cache()
    yield
    tm.clear_tenant_mirror_cache()


def _user(sub: str, org: str | None, role: str, organization_name: str | None = None) -> dict:
    u: dict = {"sub": sub, "role": "authenticated"}
    if org is not None:
        u["app_metadata"] = {"organization_id": org, "favonius_role": role}
        if organization_name is not None:
            u["app_metadata"]["organization_name"] = organization_name
    else:
        u["app_metadata"] = {"favonius_role": role} if role else {}
    return u


@pytest.mark.asyncio
async def test_mirror_inserts_org_and_membership_for_new_user():
    pool = _FakePool()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_operator",
        "Acme Transit",
    )
    await tm.mirror_user_tenant(user, pool)
    assert len(pool.conn.executes) == 2
    assert "INSERT INTO organizations" in pool.conn.executes[0][0]
    assert pool.conn.executes[0][1][1] == "Acme Transit"
    assert "INSERT INTO user_organizations" in pool.conn.executes[1][0]


@pytest.mark.asyncio
async def test_mirror_uses_placeholder_name_when_org_name_missing():
    pool = _FakePool()
    org_id = "22222222-2222-4222-8222-222222222222"
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        org_id,
        "customer_operator",
    )
    await tm.mirror_user_tenant(user, pool)
    assert len(pool.conn.executes) == 2
    assert pool.conn.executes[0][1][1] == "org-22222222"


@pytest.mark.asyncio
async def test_mirror_idempotent_on_repeat_within_ttl():
    pool = _FakePool()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_operator",
    )
    await tm.mirror_user_tenant(user, pool)
    await tm.mirror_user_tenant(user, pool)
    assert len(pool.conn.executes) == 2


@pytest.mark.asyncio
async def test_mirror_updates_role_when_jwt_role_changes():
    pool = _FakePool()
    uid = "11111111-1111-4111-8111-111111111111"
    org = "22222222-2222-4222-8222-222222222222"
    await tm.mirror_user_tenant(_user(uid, org, "customer_operator"), pool)
    assert len(pool.conn.executes) == 2
    await tm.mirror_user_tenant(_user(uid, org, "customer_admin"), pool)
    assert len(pool.conn.executes) == 4


@pytest.mark.asyncio
async def test_mirror_updates_org_when_jwt_org_changes():
    pool = _FakePool()
    uid = "11111111-1111-4111-8111-111111111111"
    await tm.mirror_user_tenant(
        _user(uid, "22222222-2222-4222-8222-222222222222", "customer_operator"),
        pool,
    )
    assert len(pool.conn.executes) == 2
    await tm.mirror_user_tenant(
        _user(uid, "33333333-3333-4333-8333-333333333333", "customer_operator"),
        pool,
    )
    assert len(pool.conn.executes) == 4


@pytest.mark.asyncio
async def test_mirror_skips_favonius_admin():
    pool = _FakePool()
    user = {
        "sub": "11111111-1111-4111-8111-111111111111",
        "app_metadata": {
            "organization_id": "22222222-2222-4222-8222-222222222222",
            "favonius_role": "favonius_admin",
        },
    }
    await tm.mirror_user_tenant(user, pool)
    assert pool.conn.executes == []


@pytest.mark.asyncio
async def test_mirror_skips_user_without_org_id():
    pool = _FakePool()
    user = {
        "sub": "11111111-1111-4111-8111-111111111111",
        "app_metadata": {"favonius_role": "customer_operator"},
    }
    await tm.mirror_user_tenant(user, pool)
    assert pool.conn.executes == []


@pytest.mark.asyncio
async def test_mirror_swallows_db_errors(caplog):
    caplog.set_level("WARNING")
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_operator",
    )
    await tm.mirror_user_tenant(user, _BrokenPool())
    assert "tenant mirror failed" in caplog.text


@pytest.mark.asyncio
async def test_mirror_no_pool_is_noop():
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_operator",
    )
    await tm.mirror_user_tenant(user, None)


@pytest.mark.asyncio
async def test_mirror_skips_missing_sub():
    pool = _FakePool()
    user = {"app_metadata": {"organization_id": "22222222-2222-4222-8222-222222222222"}}
    await tm.mirror_user_tenant(user, pool)
    assert pool.conn.executes == []


# ============ mirror_user_tenant_atomic ============


@pytest.mark.asyncio
async def test_atomic_mirror_inserts_org_and_membership_on_passed_conn():
    """``mirror_user_tenant_atomic`` runs the same UPSERTs but on the caller's
    connection so the work is part of the enclosing transaction."""
    conn = _FakeConn()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_admin",
        "HRX",
    )
    await tm.mirror_user_tenant_atomic(conn, user)
    assert len(conn.executes) == 2
    assert "INSERT INTO organizations" in conn.executes[0][0]
    assert "INSERT INTO user_organizations" in conn.executes[1][0]


@pytest.mark.asyncio
async def test_atomic_mirror_propagates_db_errors():
    """Errors must NOT be swallowed so the caller's transaction rolls back."""

    class _RaisingConn:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("simulated failure")

    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_admin",
    )
    with pytest.raises(RuntimeError, match="simulated failure"):
        await tm.mirror_user_tenant_atomic(_RaisingConn(), user)


@pytest.mark.asyncio
async def test_atomic_mirror_skips_favonius_admin():
    conn = _FakeConn()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "favonius_admin",
    )
    await tm.mirror_user_tenant_atomic(conn, user)
    assert conn.executes == []


@pytest.mark.asyncio
async def test_atomic_mirror_skips_user_without_org_id():
    conn = _FakeConn()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        None,
        "customer_admin",
    )
    await tm.mirror_user_tenant_atomic(conn, user)
    assert conn.executes == []


@pytest.mark.asyncio
async def test_atomic_mirror_does_not_prime_cache_before_commit():
    """Atomic mirror must not cache until the enclosing transaction commits;
    the next ``mirror_user_tenant`` should still run UPSERTs (no stale skip)."""
    sub = "11111111-1111-4111-8111-111111111111"
    org = "22222222-2222-4222-8222-222222222222"
    user = _user(sub, org, "customer_admin", "HRX")

    conn = _FakeConn()
    await tm.mirror_user_tenant_atomic(conn, user)
    assert len(conn.executes) == 2

    pool = _FakePool()
    await tm.mirror_user_tenant(user, pool)
    assert len(pool.conn.executes) == 2


# ============ Favonius -> Supabase role-vocab translation ============
#
# Supabase's user_organizations.role column has a CHECK constraint that only
# allows owner|admin|operator|viewer. The backend's JWT carries Favonius
# vocab (customer_admin|customer_operator|favonius_admin). Without the
# translation, every UPSERT raises CheckViolationError — silently swallowed
# by best-effort mirror_user_tenant and rolling back depot creates inside
# mirror_user_tenant_atomic. These tests pin the mapping.


@pytest.mark.parametrize(
    "favonius_role,expected_supabase_role",
    [
        ("customer_admin", "owner"),
        ("customer_operator", "operator"),
        ("favonius_admin", "admin"),
        ("authenticated", "viewer"),
        ("totally_unknown_role", "viewer"),
    ],
)
def test_supabase_role_for_translates_known_and_unknown(
    favonius_role: str, expected_supabase_role: str
) -> None:
    assert tm._supabase_role_for(favonius_role) == expected_supabase_role


@pytest.mark.asyncio
async def test_mirror_writes_supabase_role_vocab_not_favonius_vocab():
    """The role written to user_organizations must satisfy the Supabase CHECK."""
    pool = _FakePool()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_admin",
    )
    await tm.mirror_user_tenant(user, pool)
    user_org_call = pool.conn.executes[1]
    assert "INSERT INTO user_organizations" in user_org_call[0]
    written_role = user_org_call[1][2]  # third positional arg
    assert written_role == "owner"
    assert written_role not in ("customer_admin", "customer_operator", "favonius_admin")


@pytest.mark.asyncio
async def test_mirror_translates_customer_operator_to_operator():
    pool = _FakePool()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_operator",
    )
    await tm.mirror_user_tenant(user, pool)
    assert pool.conn.executes[1][1][2] == "operator"


@pytest.mark.asyncio
async def test_atomic_mirror_writes_supabase_role_vocab():
    conn = _FakeConn()
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_admin",
        "HRX",
    )
    await tm.mirror_user_tenant_atomic(conn, user)
    assert conn.executes[1][1][2] == "owner"
