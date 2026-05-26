"""Integration-test fixtures for the depot chat agent.

Spins up an isolated Postgres schema (``agent_e2e``) on the test database,
re-creates the static + time-series tables the agent touches, and seeds
two organisations with two drivers and ~50 sessions each. Both pools
(``static_pool`` / ``ts_pool``) point at the same DB but use the isolated
schema's ``search_path`` so we do not collide with the legacy migration
DDL already loaded by ``docker-compose.test.yml``.

If the test database is unreachable, every fixture skips — the unit
tests still cover the agent's pure-Python paths, and the integration
suite is opt-in once a developer has the test stack running.

The fake LLM client returned by :func:`fake_llm_client` is keyed off
short canned phrases (``"how much did <name> charge last month"``) so a
test can write ``message="how much did John charge last month"`` and the
fixture returns the matching :class:`QueryPlan` without any network I/O.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from src.api.agent.controller import LLMClient
from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow

# ── Schema bootstrap ──────────────────────────────────────────────────────


_AGENT_TEST_SCHEMA = "agent_e2e"


_DDL_BOOTSTRAP = f"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DROP SCHEMA IF EXISTS {_AGENT_TEST_SCHEMA} CASCADE;
CREATE SCHEMA {_AGENT_TEST_SCHEMA};
SET search_path TO {_AGENT_TEST_SCHEMA};

-- Static (Supabase-shaped) tables --------------------------------------
CREATE TABLE organizations (
    id   UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE sites (
    id              UUID PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    timezone        VARCHAR(50) DEFAULT 'UTC',
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    currency        VARCHAR(10) NOT NULL DEFAULT 'EUR',
    utility_id      VARCHAR(100),
    max_grid_kw     DOUBLE PRECISION,
    demand_charge_rate_kw          DOUBLE PRECISION DEFAULT 20.0,
    demand_charge_billing_period   VARCHAR(32) NOT NULL DEFAULT 'monthly',
    address                        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    billing_metadata               JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    building_load_source           JSONB NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE drivers (
    id                 UUID PRIMARY KEY,
    site_id            UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    external_driver_id VARCHAR(100),
    display_name       VARCHAR(255) NOT NULL,
    email              VARCHAR(255),
    phone              VARCHAR(64),
    status             VARCHAR(32) NOT NULL DEFAULT 'active'
);

CREATE TABLE rfid_cards (
    id      UUID PRIMARY KEY,
    site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    id_tag  VARCHAR(100) NOT NULL,
    label   VARCHAR(255),
    status  VARCHAR(32) NOT NULL DEFAULT 'active'
);

CREATE TABLE rfid_card_driver_assignments (
    card_id   UUID NOT NULL REFERENCES rfid_cards(id) ON DELETE CASCADE,
    driver_id UUID NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    PRIMARY KEY (card_id, driver_id)
);

CREATE TABLE vehicles (
    id              UUID PRIMARY KEY,
    organization_id UUID,
    site_id         UUID,
    display_name    VARCHAR(255),
    vehicle_type    VARCHAR(64),
    license_plate   VARCHAR(64),
    vin             VARCHAR(64),
    external_id     VARCHAR(100)
);

CREATE TABLE charging_stations (
    id         UUID PRIMARY KEY,
    site_id    UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    station_id VARCHAR(255) NOT NULL
);

-- Time-series tables ---------------------------------------------------
CREATE TABLE charging_sessions (
    session_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id          VARCHAR(255),
    vehicle_id          VARCHAR(255),
    driver_id           UUID,
    card_id             UUID,
    start_time          TIMESTAMPTZ NOT NULL,
    end_time            TIMESTAMPTZ,
    energy_delivered_kwh NUMERIC(10, 3),
    cost_total          NUMERIC(10, 2)
);

CREATE TABLE agent_runs (
    run_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL,
    organization_id  UUID,
    depot_id         UUID,
    user_message     TEXT NOT NULL,
    final_intent     TEXT,
    steps_json       JSONB NOT NULL DEFAULT '[]'::jsonb,
    status           TEXT NOT NULL
        CHECK (status IN ('running', 'success', 'disambiguation', 'not_found', 'error')),
    duration_ms      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_user_id   UUID,
    actor_role      VARCHAR(64),
    organization_id UUID,
    depot_id        UUID,
    action          VARCHAR(64) NOT NULL,
    target_type     VARCHAR(64),
    target_id       VARCHAR(255),
    metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb
);
"""


# Stable UUIDs make assertions readable.
ORG_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
DEPOT_A = UUID("11111111-1111-4111-8111-111111111111")
DEPOT_B = UUID("22222222-2222-4222-8222-222222222222")
USER_A = UUID("aa000000-0000-4000-8000-000000000001")
USER_B = UUID("bb000000-0000-4000-8000-000000000002")
VAN_A1 = UUID("33333333-3333-4001-8000-000000000001")
VAN_A2 = UUID("33333333-3333-4001-8000-000000000002")
VAN_B1 = UUID("33333333-3333-4002-8000-000000000001")


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )


_SCHEMA_SERVER_SETTINGS = {"search_path": f"{_AGENT_TEST_SCHEMA}, public"}


async def _bootstrap_schema() -> None:
    """Drop + recreate the agent_e2e schema, then create every table."""
    bootstrap_conn = await asyncpg.connect(_test_db_url())
    try:
        await bootstrap_conn.execute(_DDL_BOOTSTRAP)
    finally:
        await bootstrap_conn.close()


# ── DB pools (session-scoped) ─────────────────────────────────────────────


@pytest_asyncio.fixture
async def agent_db_pools():
    """Bootstrap the schema fresh and yield (static_pool, ts_pool).

    Function-scoped so each test gets independent asyncpg pools tied to
    the test's own event loop (pytest-asyncio creates a new loop per
    test by default — session-scoped pools across loops trip
    ``asyncpg.InterfaceError: another operation is in progress``).

    The bootstrap drops + recreates the schema each call so a previous
    failure cannot leak DDL or data into the next test.
    """
    try:
        await _bootstrap_schema()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"agent integration tests: test database unavailable: {exc}")

    static_pool = await asyncpg.create_pool(
        _test_db_url(),
        min_size=1,
        max_size=4,
        command_timeout=15,
        server_settings=_SCHEMA_SERVER_SETTINGS,
    )
    ts_pool = await asyncpg.create_pool(
        _test_db_url(),
        min_size=1,
        max_size=4,
        command_timeout=15,
        server_settings=_SCHEMA_SERVER_SETTINGS,
    )
    try:
        yield static_pool, ts_pool
    finally:
        await static_pool.close()
        await ts_pool.close()


@pytest_asyncio.fixture
async def seeded_db(agent_db_pools):
    """Truncate the dynamic tables and re-seed with the canned org/driver fixtures.

    Static org/site/driver/card rows are stable across tests; the
    transient tables (``charging_sessions``, ``agent_runs``, ``audit_log``)
    are wiped per test. The wipe runs *before* yielding so a previous
    failure can't leak state into the next test.
    """
    static_pool, ts_pool = agent_db_pools

    async with static_pool.acquire() as conn:
        # Wipe transient tables; static seed survives the truncate path.
        await conn.execute("TRUNCATE charging_sessions, agent_runs, audit_log")

        # Idempotent seed of the static rows.
        await conn.execute(
            """
            INSERT INTO organizations (id, name) VALUES
                ($1, 'Org A'),
                ($2, 'Org B')
            ON CONFLICT (id) DO NOTHING
            """,
            ORG_A,
            ORG_B,
        )
        await conn.execute(
            """
            INSERT INTO sites (id, organization_id, name, timezone) VALUES
                ($1, $2, 'Vilnius depot',  'Europe/Vilnius'),
                ($3, $4, 'Kaunas depot',   'Europe/Vilnius')
            ON CONFLICT (id) DO NOTHING
            """,
            DEPOT_A,
            ORG_A,
            DEPOT_B,
            ORG_B,
        )

    drivers = [
        # (driver_id, site_id, name, email, external_id)
        (
            UUID("11111111-1111-4001-8000-000000000001"),
            DEPOT_A,
            "John Smith",
            "john.smith@a.test",
            "EMP-A1",
        ),
        (
            UUID("11111111-1111-4001-8000-000000000002"),
            DEPOT_A,
            "Jane Doe",
            "jane.doe@a.test",
            "EMP-A2",
        ),
        (
            UUID("22222222-2222-4002-8000-000000000001"),
            DEPOT_B,
            "John Carter",
            "john.carter@b.test",
            "EMP-B1",
        ),
        (
            UUID("22222222-2222-4002-8000-000000000002"),
            DEPOT_B,
            "Anna Jonas",
            "anna.jonas@b.test",
            "EMP-B2",
        ),
    ]
    cards = [
        # (card_id, site_id, id_tag, label) — one card per driver
        (UUID("11111111-1111-4101-8000-000000000001"), DEPOT_A, "CARDA1", "John A card"),
        (UUID("11111111-1111-4101-8000-000000000002"), DEPOT_A, "CARDA2", "Jane card"),
        (UUID("22222222-2222-4102-8000-000000000001"), DEPOT_B, "CARDB1", "John B card"),
        (UUID("22222222-2222-4102-8000-000000000002"), DEPOT_B, "CARDB2", "Anna card"),
    ]
    assignments = list(zip([c[0] for c in cards], [d[0] for d in drivers]))

    async with static_pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO drivers (id, site_id, display_name, email, external_driver_id)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            drivers,
        )
        await conn.executemany(
            """
            INSERT INTO rfid_cards (id, site_id, id_tag, label)
            VALUES ($1::uuid, $2::uuid, $3, $4)
            ON CONFLICT (id) DO NOTHING
            """,
            cards,
        )
        await conn.executemany(
            """
            INSERT INTO rfid_card_driver_assignments (card_id, driver_id)
            VALUES ($1::uuid, $2::uuid)
            ON CONFLICT DO NOTHING
            """,
            assignments,
        )

        # Chargers: one per depot. The driver/card sessions all use
        # station_id 'TEST_STATION' (DEPOT_A), so depot-wide scoping by
        # station_id picks them up for Org A.
        await conn.executemany(
            """
            INSERT INTO charging_stations (id, site_id, station_id)
            VALUES ($1::uuid, $2::uuid, $3)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (UUID("33333333-3333-4301-8000-000000000001"), DEPOT_A, "TEST_STATION"),
                (UUID("33333333-3333-4302-8000-000000000001"), DEPOT_B, "TEST_STATION_B"),
            ],
        )

        # A Renault van fleet at DEPOT_A (Org A) for the fleet path, plus
        # one Org B vehicle to prove cross-org isolation.
        await conn.executemany(
            """
            INSERT INTO vehicles (id, organization_id, site_id, display_name, vehicle_type)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (VAN_A1, ORG_A, DEPOT_A, "Renault Van 1", "van"),
                (VAN_A2, ORG_A, DEPOT_A, "Renault Van 2", "van"),
                (VAN_B1, ORG_B, DEPOT_B, "Renault Van B", "van"),
            ],
        )

    # ── Charging sessions: 25 per driver across the past 30 days ─────────
    # The test only references "last_month" / "this_month" windows, so the
    # range starts 35 days ago and ends 5 days ago. 100 rows total.
    base = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    ) - timedelta(days=35)
    rows: list[tuple[Any, ...]] = []
    for driver_idx, (driver_id, _site, _name, _email, _ext) in enumerate(drivers):
        card_id = cards[driver_idx][0]
        for i in range(25):
            ts = base + timedelta(days=(i * 30) // 25, hours=(i % 4))
            rows.append(
                (
                    "TEST_STATION",
                    driver_id,
                    card_id,
                    ts,
                    ts + timedelta(hours=1),
                    1.0 + i * 0.5,
                    0.20 + i * 0.05,
                )
            )
    async with ts_pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO charging_sessions (
                station_id, driver_id, card_id,
                start_time, end_time,
                energy_delivered_kwh, cost_total
            )
            VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7)
            """,
            rows,
        )

        # Vehicle/fleet sessions: each Org A Renault van charges a few times
        # in the last_month window, keyed by vehicle_id (UUID as text, as
        # both the live and import write paths store it).
        van_rows: list[tuple[Any, ...]] = []
        for van_id in (VAN_A1, VAN_A2):
            for i in range(3):
                ts = base + timedelta(days=2 + i, hours=i)
                van_rows.append(
                    ("TEST_STATION", str(van_id), ts, ts + timedelta(hours=1), 5.0 + i, 1.0 + i)
                )
        await conn.executemany(
            """
            INSERT INTO charging_sessions (
                station_id, vehicle_id,
                start_time, end_time,
                energy_delivered_kwh, cost_total
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            van_rows,
        )

    yield {
        "static_pool": static_pool,
        "ts_pool": ts_pool,
        "drivers": drivers,
        "cards": cards,
        "org_a": ORG_A,
        "org_b": ORG_B,
        "depot_a": DEPOT_A,
        "depot_b": DEPOT_B,
        "user_a": USER_A,
        "user_b": USER_B,
        "van_a1": VAN_A1,
        "van_a2": VAN_A2,
        "van_b1": VAN_B1,
    }


# ── JWT payload builders ──────────────────────────────────────────────────


def make_token_payload(
    user_id: UUID,
    *,
    organization_id: Optional[UUID],
    role: str = "customer_admin",
) -> dict[str, Any]:
    """Build a token-payload dict that build_auth_context accepts."""
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": "authenticated",
        "app_metadata": {"favonius_role": role},
    }
    if organization_id is not None:
        payload["app_metadata"]["organization_id"] = str(organization_id)
    return payload


# ── Fake LLM client ───────────────────────────────────────────────────────


class FakeLLMClient(LLMClient):
    """Deterministic stand-in for the Anthropic-backed :class:`LLMClient`.

    The orchestrator passes every user message through ``extract_plan``
    and the SQL result set through ``format_answer``. This fake matches
    on substrings of the message so a test reads
    ``await client.run_turn("how much did John charge last month")``
    without any I/O. ``raise_on_extract`` lets the error-path test force
    the orchestrator into its except-branch without touching the real
    Anthropic client.
    """

    def __init__(
        self,
        *,
        raise_on_extract: bool = False,
        canned_answer: str = "Here is your answer.",
    ) -> None:
        self.raise_on_extract = raise_on_extract
        self.canned_answer = canned_answer
        self.last_format_payload: Optional[dict[str, Any]] = None

    async def extract_plan(self, message: str) -> QueryPlan:
        if self.raise_on_extract:
            raise RuntimeError("simulated LLM upstream failure")
        m = message.lower()

        # Out-of-scope / non-name search phrase.
        if "zorblax" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="driver", text="Zorblax")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        if "carter" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="driver", text="Carter")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        # Depot-scoped total: a named depot subject ("at the Vilnius depot").
        # Checked before the depot-wide branch so the named-depot phrasing
        # routes through the subject path (resolve → depot-scoped total).
        if "vilnius" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="depot", text="Vilnius depot")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        # Depot-wide total: no named subject (depot_wide signal).
        if "total" in m or "depot-wide" in m or "was consumed" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[],
                time_window=TimeWindow(kind="relative", relative="last_month"),
                depot_wide=True,
            )
        # Vehicle / fleet mention.
        if "renault" in m or "van" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="vehicle", text="renault van")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        if "jane" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="driver", text="Jane")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        # The two-Johns disambiguation prompt looks identical to the
        # single-John happy path on the LLM side; the resolver decides
        # whether to disambiguate based on visible_depot_ids.
        if "john" in m:
            return QueryPlan(
                intent="consumption_by_user",
                subjects=[EntityMention(kind="driver", text="John")],
                time_window=TimeWindow(kind="relative", relative="last_month"),
            )
        # Default: a no-op plan with an empty subjects list (refusal).
        return QueryPlan(
            intent="consumption_by_user",
            subjects=[EntityMention(kind="driver", text=message[:32])],
            time_window=TimeWindow(kind="relative", relative="last_month"),
        )

    async def format_answer(
        self,
        plan: QueryPlan,
        resolved: list[dict[str, Any]],
        window: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        result_summary: Optional[dict[str, Any]] = None,
    ) -> str:
        # Stash for assertion in tests that need to inspect what the
        # formatter saw.
        self.last_format_payload = {
            "intent": plan.intent,
            "depot_wide": plan.depot_wide,
            "resolved": resolved,
            "window": window,
            "row_count": len(rows),
            "result_summary": result_summary,
        }
        return self.canned_answer


@pytest.fixture
def fake_llm_client() -> FakeLLMClient:
    """Default fake LLM client. Tests can mutate fields before the call."""
    return FakeLLMClient()
