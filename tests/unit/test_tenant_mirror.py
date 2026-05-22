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


# ============ repair_user_tenant_metadata ============
#
# Self-heal path: a verified JWT lacks app_metadata.organization_id but the
# user has exactly one user_organizations row. The repair pushes the derived
# claims back to Supabase via the Auth admin API so the user's NEXT token
# refresh sees correct claims and the first-depot wizard becomes visible.


class _FetchableConn:
    """``asyncpg.Connection`` stand-in that supports both ``execute`` and
    ``fetch``. ``fetch`` returns canned rows (or raises if configured)."""

    def __init__(self, fetch_rows=None, raise_on_fetch=None):
        self._rows = fetch_rows if fetch_rows is not None else []
        self._raise = raise_on_fetch
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, q, *args):
        self.executes.append((q, tuple(args)))

    async def fetch(self, q, *args):
        self.fetch_calls.append((q, tuple(args)))
        if self._raise is not None:
            raise self._raise
        return self._rows


class _FetchablePool:
    def __init__(self, fetch_rows=None, raise_on_fetch=None):
        self.conn = _FetchableConn(fetch_rows, raise_on_fetch)

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.fixture
def _supabase_env(monkeypatch):
    """Set Supabase admin-API env vars; the repair is gated on both."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")


@pytest.fixture
def _record_admin_calls(monkeypatch):
    """Replace the httpx admin-API helper with an in-memory recorder."""
    calls: list[dict] = []

    async def _fake(*, supabase_url, service_key, user_id, app_metadata, **_):
        calls.append(
            {
                "supabase_url": supabase_url,
                "service_key": service_key,
                "user_id": user_id,
                "app_metadata": app_metadata,
            }
        )

    monkeypatch.setattr(tm, "_patch_supabase_app_metadata", _fake)
    return calls


def _orphaned_user(sub: str) -> dict:
    """JWT whose app_metadata is missing tenancy claims (the broken-signup case)."""
    return {
        "sub": sub,
        "role": "authenticated",
        "app_metadata": {"provider": "email", "providers": ["email"]},
    }


def _user_with_org_no_role(sub: str, org_id: str, organization_name: str | None = None) -> dict:
    """JWT with organization_id set but favonius_role absent (Case B / Gustas pattern)."""
    meta: dict = {"provider": "email", "providers": ["email"], "organization_id": org_id}
    if organization_name is not None:
        meta["organization_name"] = organization_name
    return {"sub": sub, "role": "authenticated", "app_metadata": meta}


@pytest.mark.asyncio
async def test_repair_backfills_app_metadata_for_owner_membership(
    _supabase_env, _record_admin_calls
):
    """Happy path: 1 user_organizations row with role=owner → admin API called
    with customer_admin and the org_id derived from the row."""
    sub = "d24f55e8-2ee1-4d09-88a6-0811bdb3411b"
    org_id = "d1288ddc-c696-4a94-b70b-7368f674580b"
    pool = _FetchablePool([{"organization_id": org_id, "role": "owner", "name": "HRX, UAB"}])

    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)

    assert len(_record_admin_calls) == 1
    call = _record_admin_calls[0]
    assert call["user_id"] == sub
    assert call["supabase_url"] == "https://test.supabase.co"
    assert call["service_key"] == "test-service-key"
    assert call["app_metadata"] == {
        "favonius_role": "customer_admin",
        "organization_id": org_id,
        "organization_name": "HRX, UAB",
    }


@pytest.mark.asyncio
async def test_repair_backfills_customer_operator_for_operator_membership(
    _supabase_env, _record_admin_calls
):
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"organization_id": org_id, "role": "operator", "name": "Acme"}])

    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)

    assert len(_record_admin_calls) == 1
    assert _record_admin_calls[0]["app_metadata"]["favonius_role"] == "customer_operator"


@pytest.mark.asyncio
async def test_repair_uses_placeholder_when_org_name_is_null(_supabase_env, _record_admin_calls):
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"organization_id": org_id, "role": "owner", "name": None}])

    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)

    assert len(_record_admin_calls) == 1
    assert _record_admin_calls[0]["app_metadata"]["organization_name"] == "org-22222222"


@pytest.mark.asyncio
async def test_repair_noop_when_jwt_already_has_org_id(_supabase_env, _record_admin_calls):
    """Healthy JWT must not trigger any admin call or DB lookup."""
    pool = _FetchablePool([{"organization_id": "x", "role": "owner", "name": "y"}])
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_admin",
    )
    await tm.repair_user_tenant_metadata(user, pool)
    assert _record_admin_calls == []
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_repair_noop_for_favonius_admin(_supabase_env, _record_admin_calls):
    """Favonius staff legitimately have no organization_id; never auto-repair them."""
    pool = _FetchablePool([{"organization_id": "x", "role": "owner", "name": "y"}])
    user = {
        "sub": "11111111-1111-4111-8111-111111111111",
        "app_metadata": {"favonius_role": "favonius_admin"},
    }
    await tm.repair_user_tenant_metadata(user, pool)
    assert _record_admin_calls == []
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_repair_noop_when_sub_missing(_supabase_env, _record_admin_calls):
    pool = _FetchablePool([{"organization_id": "x", "role": "owner", "name": "y"}])
    await tm.repair_user_tenant_metadata({"app_metadata": {}}, pool)
    assert _record_admin_calls == []
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_repair_noop_when_pool_is_none(_supabase_env, _record_admin_calls):
    await tm.repair_user_tenant_metadata(
        _orphaned_user("d24f55e8-2ee1-4d09-88a6-0811bdb3411b"), None
    )
    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_noop_when_supabase_env_missing(monkeypatch, _record_admin_calls):
    """Without SUPABASE_URL or SUPABASE_SERVICE_KEY the repair must skip
    silently — dev/local environments have no admin credentials."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    pool = _FetchablePool([{"organization_id": "x", "role": "owner", "name": "y"}])
    await tm.repair_user_tenant_metadata(
        _orphaned_user("11111111-1111-4111-8111-111111111111"), pool
    )
    assert _record_admin_calls == []
    # Skip happens before the DB lookup — don't even touch user_organizations.
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_repair_noop_when_user_has_no_membership(_supabase_env, _record_admin_calls):
    pool = _FetchablePool([])
    await tm.repair_user_tenant_metadata(
        _orphaned_user("11111111-1111-4111-8111-111111111111"), pool
    )
    assert _record_admin_calls == []
    # DB lookup happened, but no admin call.
    assert len(pool.conn.fetch_calls) == 1


@pytest.mark.asyncio
async def test_repair_noop_when_user_has_multiple_memberships(_supabase_env, _record_admin_calls):
    """Multiple user_organizations rows are ambiguous — we cannot pick a single
    org to backfill, so skip and let an operator resolve it manually."""
    pool = _FetchablePool(
        [
            {"organization_id": "a", "role": "owner", "name": "A"},
            {"organization_id": "b", "role": "owner", "name": "B"},
        ]
    )
    await tm.repair_user_tenant_metadata(
        _orphaned_user("11111111-1111-4111-8111-111111111111"), pool
    )
    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_skips_admin_membership_role(_supabase_env, _record_admin_calls):
    """A user_organizations row with role='admin' (Favonius staff) must NOT
    auto-promote the user to favonius_admin — that would let a corrupted DB
    row escalate privilege via the self-heal path."""
    pool = _FetchablePool([{"organization_id": "x", "role": "admin", "name": "Staff"}])
    await tm.repair_user_tenant_metadata(
        _orphaned_user("11111111-1111-4111-8111-111111111111"), pool
    )
    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_skips_viewer_membership_role(_supabase_env, _record_admin_calls):
    """'viewer' has no Favonius-vocab equivalent, so we don't auto-derive."""
    pool = _FetchablePool([{"organization_id": "x", "role": "viewer", "name": "Y"}])
    await tm.repair_user_tenant_metadata(
        _orphaned_user("11111111-1111-4111-8111-111111111111"), pool
    )
    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_caches_within_ttl(_supabase_env, _record_admin_calls):
    """Second repair call within the TTL must not re-issue the admin call."""
    sub = "11111111-1111-4111-8111-111111111111"
    pool = _FetchablePool([{"organization_id": "y", "role": "owner", "name": "Y"}])
    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)
    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)
    assert len(_record_admin_calls) == 1
    # Second call should not even hit the DB once cached.
    assert len(pool.conn.fetch_calls) == 1


@pytest.mark.asyncio
async def test_repair_caches_negative_result_to_avoid_repeated_db_scans(
    _supabase_env, _record_admin_calls
):
    """When membership lookup returns no rows the result is also cached, so
    we don't scan ``user_organizations`` on every request for a user who
    legitimately has no org yet."""
    sub = "11111111-1111-4111-8111-111111111111"
    pool = _FetchablePool([])
    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)
    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)
    assert len(pool.conn.fetch_calls) == 1
    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_does_not_cache_on_admin_api_failure(_supabase_env, monkeypatch, caplog):
    """Transient admin-API failures must NOT lock the user out of repair for a
    full TTL; the next request retries."""
    caplog.set_level("WARNING")

    call_count = {"n": 0}

    async def _flaky(**_kwargs):
        call_count["n"] += 1
        raise RuntimeError("admin API down")

    monkeypatch.setattr(tm, "_patch_supabase_app_metadata", _flaky)
    sub = "11111111-1111-4111-8111-111111111111"
    pool = _FetchablePool([{"organization_id": "y", "role": "owner", "name": "Y"}])

    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)
    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)

    assert call_count["n"] == 2
    assert "tenant_metadata_repair: admin API call failed" in caplog.text


@pytest.mark.asyncio
async def test_repair_swallows_db_lookup_errors(_supabase_env, _record_admin_calls, caplog):
    caplog.set_level("WARNING")
    pool = _FetchablePool(raise_on_fetch=RuntimeError("db down"))
    await tm.repair_user_tenant_metadata(
        _orphaned_user("11111111-1111-4111-8111-111111111111"), pool
    )
    assert _record_admin_calls == []
    assert "tenant_metadata_repair: db lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_repair_logs_success_at_warning_level(_supabase_env, _record_admin_calls, caplog):
    """The successful repair logs a structured WARNING so ops can see how often
    the safety net is firing — high counts signal a frontend-signup regression."""
    caplog.set_level("WARNING")
    sub = "d24f55e8-2ee1-4d09-88a6-0811bdb3411b"
    pool = _FetchablePool(
        [
            {
                "organization_id": "d1288ddc-c696-4a94-b70b-7368f674580b",
                "role": "owner",
                "name": "HRX, UAB",
            }
        ]
    )
    await tm.repair_user_tenant_metadata(_orphaned_user(sub), pool)
    assert "tenant_metadata_repair: backfilled app_metadata" in caplog.text


@pytest.mark.asyncio
async def test_ensure_tenant_mirrored_runs_repair_before_mirror(
    _supabase_env, _record_admin_calls, monkeypatch
):
    """Integration: the FastAPI dependency calls repair, then mirror — both
    are best-effort and neither one breaks auth on failure."""
    sub = "11111111-1111-4111-8111-111111111111"
    pool = _FetchablePool([{"organization_id": "y", "role": "owner", "name": "Y"}])

    # Patch api_main.db_pools so ensure_tenant_mirrored uses our fake pool.
    from src.api import main as api_main

    class _Pools:
        static = pool

    monkeypatch.setattr(api_main, "db_pools", _Pools())

    user = _orphaned_user(sub)
    returned = await tm.ensure_tenant_mirrored(user)
    assert returned is user  # same dict object is returned
    assert len(_record_admin_calls) == 1  # Supabase was patched
    # Mirror runs on the now-corrected claims: org UPSERT + membership UPSERT.
    assert len(pool.conn.executes) == 2


# ============ repair — in-place user dict mutation ============


@pytest.mark.asyncio
async def test_repair_patches_user_dict_in_place_case_a(_supabase_env, _record_admin_calls):
    """Case A: after repair the in-flight user dict carries the derived claims so
    the current request's auth check sees the corrected role immediately."""
    sub = "d24f55e8-2ee1-4d09-88a6-0811bdb3411b"
    org_id = "d1288ddc-c696-4a94-b70b-7368f674580b"
    pool = _FetchablePool([{"organization_id": org_id, "role": "owner", "name": "HRX, UAB"}])
    user = _orphaned_user(sub)

    await tm.repair_user_tenant_metadata(user, pool)

    assert user["app_metadata"]["favonius_role"] == "customer_admin"
    assert user["app_metadata"]["organization_id"] == org_id
    assert user["app_metadata"]["organization_name"] == "HRX, UAB"


@pytest.mark.asyncio
async def test_repair_patches_user_dict_in_place_case_b(_supabase_env, _record_admin_calls):
    """Case B: after repair favonius_role is written to the user dict without
    overwriting organization_id (which was already correct)."""
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "owner", "name": "HRX, UAB"}])
    user = _user_with_org_no_role(sub, org_id)

    await tm.repair_user_tenant_metadata(user, pool)

    assert user["app_metadata"]["favonius_role"] == "customer_admin"
    assert user["app_metadata"]["organization_id"] == org_id  # untouched


@pytest.mark.asyncio
async def test_repair_does_not_mutate_user_dict_on_api_failure(_supabase_env, monkeypatch):
    """If the Supabase admin API call fails the user dict must NOT be mutated —
    a 403 is better than silently granting a role that wasn't persisted."""
    async def _fail(**_kwargs):
        raise RuntimeError("admin API down")

    monkeypatch.setattr(tm, "_patch_supabase_app_metadata", _fail)
    sub = "11111111-1111-4111-8111-111111111111"
    pool = _FetchablePool([{"organization_id": "y", "role": "owner", "name": "Y"}])
    user = _orphaned_user(sub)
    original_meta = dict(user["app_metadata"])

    await tm.repair_user_tenant_metadata(user, pool)

    assert user["app_metadata"] == original_meta


# ============ _fetch_single_user_org_membership ============


@pytest.mark.asyncio
async def test_fetch_membership_returns_none_for_zero_rows():
    conn = _FetchableConn([])
    assert await tm._fetch_single_user_org_membership(conn, "uid") is None


@pytest.mark.asyncio
async def test_fetch_membership_returns_none_for_multiple_rows():
    conn = _FetchableConn(
        [
            {"organization_id": "a", "role": "owner", "name": "A"},
            {"organization_id": "b", "role": "owner", "name": "B"},
        ]
    )
    assert await tm._fetch_single_user_org_membership(conn, "uid") is None


@pytest.mark.asyncio
async def test_fetch_membership_returns_none_for_unrepairable_role():
    conn = _FetchableConn([{"organization_id": "a", "role": "viewer", "name": "A"}])
    assert await tm._fetch_single_user_org_membership(conn, "uid") is None


@pytest.mark.asyncio
async def test_fetch_membership_returns_tuple_for_owner():
    conn = _FetchableConn([{"organization_id": "abc", "role": "owner", "name": "Acme"}])
    result = await tm._fetch_single_user_org_membership(conn, "uid")
    assert result == ("abc", "owner", "Acme")


@pytest.mark.asyncio
async def test_fetch_membership_normalizes_blank_org_name_to_none():
    conn = _FetchableConn([{"organization_id": "abc", "role": "owner", "name": "   "}])
    result = await tm._fetch_single_user_org_membership(conn, "uid")
    assert result == ("abc", "owner", None)


# ============ repair_user_tenant_metadata — Case B ============
#
# Case B: JWT carries organization_id but favonius_role is absent.
# This is the Gustas/HRX pattern: signup provisioned the org claim but
# Supabase never wrote favonius_role into app_metadata. The self-heal
# must push just the role (and org_name when also absent), without
# overwriting organization_id which is already correct.


@pytest.mark.asyncio
async def test_repair_case_b_backfills_favonius_role_for_owner(
    _supabase_env, _record_admin_calls
):
    """Happy path: org_id present, favonius_role absent, DB has owner row → push role only."""
    sub = "d24f55e8-2ee1-4d09-88a6-0811bdb3411b"
    org_id = "d1288ddc-c696-4a94-b70b-7368f674580b"
    pool = _FetchablePool([{"role": "owner", "name": "HRX, UAB"}])

    await tm.repair_user_tenant_metadata(_user_with_org_no_role(sub, org_id), pool)

    assert len(_record_admin_calls) == 1
    call = _record_admin_calls[0]
    assert call["user_id"] == sub
    assert call["app_metadata"]["favonius_role"] == "customer_admin"
    assert "organization_id" not in call["app_metadata"]


@pytest.mark.asyncio
async def test_repair_case_b_backfills_customer_operator_for_operator(
    _supabase_env, _record_admin_calls
):
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "operator", "name": "Acme"}])

    await tm.repair_user_tenant_metadata(_user_with_org_no_role(sub, org_id), pool)

    assert _record_admin_calls[0]["app_metadata"]["favonius_role"] == "customer_operator"


@pytest.mark.asyncio
async def test_repair_case_b_also_backfills_org_name_when_absent(
    _supabase_env, _record_admin_calls
):
    """When organization_name is also missing from the JWT, include it in the payload."""
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "owner", "name": "HRX, UAB"}])

    await tm.repair_user_tenant_metadata(_user_with_org_no_role(sub, org_id), pool)

    call = _record_admin_calls[0]
    assert call["app_metadata"]["organization_name"] == "HRX, UAB"


@pytest.mark.asyncio
async def test_repair_case_b_skips_org_name_when_already_set_in_jwt(
    _supabase_env, _record_admin_calls
):
    """Do not overwrite an organization_name that's already correct in the JWT."""
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "owner", "name": "HRX, UAB"}])

    user = _user_with_org_no_role(sub, org_id, organization_name="Already Set")
    await tm.repair_user_tenant_metadata(user, pool)

    call = _record_admin_calls[0]
    assert "organization_name" not in call["app_metadata"]


@pytest.mark.asyncio
async def test_repair_case_b_noop_when_no_membership_for_org(
    _supabase_env, _record_admin_calls
):
    """No user_organizations row for the (user, org) pair → no admin call."""
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([])  # empty fetch result

    await tm.repair_user_tenant_metadata(_user_with_org_no_role(sub, org_id), pool)

    assert _record_admin_calls == []
    assert len(pool.conn.fetch_calls) == 1


@pytest.mark.asyncio
async def test_repair_case_b_noop_for_admin_role(_supabase_env, _record_admin_calls):
    """A user_organizations row with role='admin' must not auto-promote via Case B."""
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "admin", "name": "Staff"}])

    await tm.repair_user_tenant_metadata(_user_with_org_no_role(sub, org_id), pool)

    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_case_b_noop_for_viewer_role(_supabase_env, _record_admin_calls):
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "viewer", "name": "View Only"}])

    await tm.repair_user_tenant_metadata(_user_with_org_no_role(sub, org_id), pool)

    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_case_b_caches_success_within_ttl(_supabase_env, _record_admin_calls):
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([{"role": "owner", "name": "Y"}])
    user = _user_with_org_no_role(sub, org_id)

    await tm.repair_user_tenant_metadata(user, pool)
    await tm.repair_user_tenant_metadata(user, pool)

    assert len(_record_admin_calls) == 1
    assert len(pool.conn.fetch_calls) == 1


@pytest.mark.asyncio
async def test_repair_case_b_caches_negative_result(_supabase_env, _record_admin_calls):
    sub = "11111111-1111-4111-8111-111111111111"
    org_id = "22222222-2222-4222-8222-222222222222"
    pool = _FetchablePool([])
    user = _user_with_org_no_role(sub, org_id)

    await tm.repair_user_tenant_metadata(user, pool)
    await tm.repair_user_tenant_metadata(user, pool)

    assert len(pool.conn.fetch_calls) == 1
    assert _record_admin_calls == []


@pytest.mark.asyncio
async def test_repair_case_b_does_not_fire_when_both_claims_present(
    _supabase_env, _record_admin_calls
):
    """Healthy JWT (org_id + favonius_role both set) must skip both DB and admin call."""
    pool = _FetchablePool([{"role": "owner", "name": "Y"}])
    user = _user(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "customer_admin",
    )
    await tm.repair_user_tenant_metadata(user, pool)
    assert _record_admin_calls == []
    assert pool.conn.fetch_calls == []


# ============ _fetch_role_for_org ============


@pytest.mark.asyncio
async def test_fetch_role_for_org_returns_none_for_no_rows():
    conn = _FetchableConn([])
    assert await tm._fetch_role_for_org(conn, "uid", "org") is None


@pytest.mark.asyncio
async def test_fetch_role_for_org_returns_none_for_unrepairable_role():
    conn = _FetchableConn([{"role": "admin", "name": "Staff"}])
    assert await tm._fetch_role_for_org(conn, "uid", "org") is None


@pytest.mark.asyncio
async def test_fetch_role_for_org_returns_tuple_for_owner():
    conn = _FetchableConn([{"role": "owner", "name": "HRX, UAB"}])
    result = await tm._fetch_role_for_org(conn, "uid", "org")
    assert result == ("owner", "HRX, UAB")


@pytest.mark.asyncio
async def test_fetch_role_for_org_normalizes_blank_name_to_none():
    conn = _FetchableConn([{"role": "operator", "name": "  "}])
    result = await tm._fetch_role_for_org(conn, "uid", "org")
    assert result == ("operator", None)


# ============ Reverse role-vocab mapping ============


@pytest.mark.parametrize(
    "supabase_role,expected_favonius_role",
    [
        ("owner", "customer_admin"),
        ("operator", "customer_operator"),
    ],
)
def test_supabase_to_favonius_role_mapping(supabase_role: str, expected_favonius_role: str) -> None:
    assert tm._SUPABASE_TO_FAVONIUS_ROLE[supabase_role] == expected_favonius_role


def test_supabase_to_favonius_excludes_admin_and_viewer() -> None:
    """'admin' (favonius_admin) is excluded so a corrupted user_organizations
    row cannot escalate to staff via the self-heal path. 'viewer' has no
    Favonius equivalent."""
    assert "admin" not in tm._SUPABASE_TO_FAVONIUS_ROLE
    assert "viewer" not in tm._SUPABASE_TO_FAVONIUS_ROLE
