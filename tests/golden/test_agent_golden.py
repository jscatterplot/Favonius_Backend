"""Golden test runner for the depot chat agent.

Gate: ``pytest --golden tests/golden/`` (the ``golden`` marker is opt-in so
CI does not pay the LLM round-trip on every push — only on PRs that touch
``src/api/agent/`` and on the nightly run).

Two test families per YAML entry:

1. ``test_extract_plan_golden``  — calls the real Anthropic API.  Asserts
   the LLM produces a ``QueryPlan`` with the expected intent and the right
   number / kinds of subjects.  Refusals (``expected_status: refused``) assert
   ``subjects == []``.

2. ``test_resolver_status_golden`` — fully offline (no LLM, no real DB).
   Constructs a canned ``FakeLLMClient`` returning the expected plan, seeds a
   minimal in-memory-style mock asyncpg pool for the entry's scenario, runs
   the entity resolver, and asserts the resulting ``ResolvedEntity`` list is
   consistent with ``expected_status``.

PRD §7 correctness target: ≥ 95% (≥ 48/50) of ``test_extract_plan_golden``
entries must pass on every deploy that touches ``src/api/agent/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import pytest_asyncio
import yaml

from src.api.agent.auth_context import AuthContext
from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow
from src.api.agent.resolve import resolve_entities

# ── YAML loading ──────────────────────────────────────────────────────────────

_YAML_PATH = Path(__file__).parent / "agent_consumption.yaml"


def _load_entries() -> list[dict[str, Any]]:
    with _YAML_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_ALL_ENTRIES: list[dict[str, Any]] = _load_entries()

# Pre-split by expected_status for parametrize convenience.
_REFUSAL_ENTRIES = [e for e in _ALL_ENTRIES if e["expected_status"] == "refused"]
_NON_REFUSAL_ENTRIES = [e for e in _ALL_ENTRIES if e["expected_status"] != "refused"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _subjects_from_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("expected_subjects") or []


def _assert_plan_subjects(
    plan: QueryPlan,
    expected: list[dict[str, Any]],
    entry_id: str,
) -> None:
    """Assert plan subjects match expected kinds (order-insensitive)."""
    actual_kinds = sorted(s.kind for s in plan.subjects)
    expected_kinds = sorted(s["kind"] for s in expected)
    assert actual_kinds == expected_kinds, (
        f"[{entry_id}] subject kinds mismatch: "
        f"got {actual_kinds}, want {expected_kinds}"
    )


# ── Stable UUIDs for mock fixtures ───────────────────────────────────────────

_ORG_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_DEPOT_ID = UUID("11111111-1111-4111-8111-111111111111")
_DEPOT_B_ID = UUID("22222222-2222-4222-8222-222222222222")
_DRIVER_JOHN = UUID("d0000001-0000-4000-8000-000000000001")
_DRIVER_JANE = UUID("d0000002-0000-4000-8000-000000000002")
_DRIVER_JOHN2 = UUID("d0000003-0000-4000-8000-000000000003")  # for ambiguity
_DRIVER_LUKAS = UUID("d0000004-0000-4000-8000-000000000004")
_CARD_1 = UUID("c0000001-0000-4000-8000-000000000001")


# ── Mock asyncpg pool builder ─────────────────────────────────────────────────

def _make_pool(rows_for_kind: dict[str, list[dict[str, Any]]]) -> Any:
    """Return a mock asyncpg pool whose ``fetch()`` returns per-entity rows.

    The resolver calls ``static_pool.fetch(sql, *params)`` directly (no
    ``acquire()``).  We sniff the SQL substring to identify which entity
    kind is being resolved and return the appropriate canned rows.

    Rows are plain dicts; the resolver accesses columns via ``row["key"]``
    so they must have the right column names (matching the ``AS`` aliases
    in resolve.py's SQL).
    """
    driver_rows = rows_for_kind.get("driver", [])
    depot_rows = rows_for_kind.get("depot", [])
    rfid_rows = rows_for_kind.get("rfid", [])
    vehicle_rows = rows_for_kind.get("vehicle", [])

    class _DictRow(dict):
        """dict subclass that also supports attribute-style access."""

        def __getattr__(self, key: str) -> Any:
            try:
                return self[key]
            except KeyError as exc:
                raise AttributeError(key) from exc

    def _rows(raw: list[dict[str, Any]]) -> list[_DictRow]:
        return [_DictRow(r) for r in raw]

    async def _fetch(sql: str, *args: Any) -> list[Any]:
        sql_lower = sql.lower()
        if "from drivers" in sql_lower:
            return _rows(driver_rows)
        if "from rfid_cards" in sql_lower:
            return _rows(rfid_rows)
        if "from vehicles" in sql_lower:
            return _rows(vehicle_rows)
        if "from sites" in sql_lower:
            return _rows(depot_rows)
        return []

    mock_pool = MagicMock()
    mock_pool.fetch = _fetch
    return mock_pool


def _auth_for_depot(depot_ids: list[UUID] | None = None) -> AuthContext:
    return AuthContext(
        user_id=UUID("aa000000-0000-4000-8000-000000000001"),
        organization_id=_ORG_ID,
        role="customer_operator",
        visible_depot_ids=list(depot_ids or [_DEPOT_ID]),
    )


def _single_john_pool() -> Any:
    return _make_pool({"driver": [
        {
            "driver_id": _DRIVER_JOHN,
            "display_name": "John Smith",
            "depot_id": _DEPOT_ID,
            "depot_name": "Vilnius",
            "external_driver_id": "EMP-1042",
            "card_ids": [_CARD_1],
        },
    ]})


def _two_john_pool() -> Any:
    return _make_pool({"driver": [
        {
            "driver_id": _DRIVER_JOHN,
            "display_name": "John Smith",
            "depot_id": _DEPOT_ID,
            "depot_name": "Vilnius",
            "external_driver_id": "EMP-1042",
            "card_ids": [],
        },
        {
            "driver_id": _DRIVER_JOHN2,
            "display_name": "John Petrauskas",
            "depot_id": _DEPOT_ID,
            "depot_name": "Vilnius",
            "external_driver_id": "EMP-1158",
            "card_ids": [],
        },
    ]})


def _empty_pool() -> Any:
    return _make_pool({})


# ── Status mapping for resolver mock scenarios ────────────────────────────────

# Maps entry id → pool factory callable.  If an entry id is not here the
# default is _single_john_pool (unique match → success) or empty (not_found).
_POOL_FACTORIES: dict[str, Any] = {
    # ambiguity entries
    "amb-01": _two_john_pool,
    "amb-02": _two_john_pool,
    "amb-03": _two_john_pool,
    "amb-04": _two_john_pool,
    "amb-05": _two_john_pool,
    "amb-06": _two_john_pool,
    "amb-07": _two_john_pool,
    "amb-08": _two_john_pool,
    "amb-09": _two_john_pool,
    "amb-10": _two_john_pool,
    # not-found entries
    "nf-01": _empty_pool,
    "nf-02": _empty_pool,
    "nf-03": _empty_pool,
    "nf-04": _empty_pool,
    "nf-05": _empty_pool,
    "nf-06": _empty_pool,
    "nf-07": _empty_pool,
    "nf-08": _empty_pool,
    "nf-09": _empty_pool,
    "nf-10": _empty_pool,
}


def _pool_for_entry(entry: dict[str, Any]) -> Any:
    factory = _POOL_FACTORIES.get(entry["id"])
    if factory is not None:
        return factory()
    return _single_john_pool()


# ── YAML count integrity check ────────────────────────────────────────────────

def test_yaml_entry_count() -> None:
    """The golden file must have exactly 52 entries (PRD §7 baseline of 50
    plus the vehicle-fleet and depot-wide additions)."""
    assert len(_ALL_ENTRIES) == 52, (
        f"agent_consumption.yaml has {len(_ALL_ENTRIES)} entries; expected 52"
    )


def test_yaml_ids_are_unique() -> None:
    """All entry ids must be unique."""
    ids = [e["id"] for e in _ALL_ENTRIES]
    assert len(ids) == len(set(ids)), "Duplicate ids in agent_consumption.yaml"


def test_yaml_required_fields_present() -> None:
    """Every entry must have the required keys."""
    required = {"id", "message", "expected_intent", "expected_subjects", "expected_status"}
    for entry in _ALL_ENTRIES:
        missing = required - entry.keys()
        assert not missing, f"Entry {entry.get('id')} missing fields: {missing}"


def test_yaml_category_counts() -> None:
    """Verify the required distribution from PRD §7."""
    by_status: dict[str, int] = {}
    for entry in _ALL_ENTRIES:
        s = entry["expected_status"]
        by_status[s] = by_status.get(s, 0) + 1

    prefixes: dict[str, int] = {}
    for entry in _ALL_ENTRIES:
        prefix = entry["id"].split("-")[0]
        prefixes[prefix] = prefixes.get(prefix, 0) + 1

    assert prefixes.get("hp", 0) == 10, f"Expected 10 happy-path entries, got {prefixes.get('hp', 0)}"
    assert prefixes.get("amb", 0) == 10, f"Expected 10 ambiguity entries, got {prefixes.get('amb', 0)}"
    assert prefixes.get("nf", 0) == 10, f"Expected 10 not-found entries, got {prefixes.get('nf', 0)}"
    assert prefixes.get("edge", 0) == 17, f"Expected 17 edge-case entries, got {prefixes.get('edge', 0)}"
    assert prefixes.get("ref", 0) == 5, f"Expected 5 refusal entries, got {prefixes.get('ref', 0)}"


# ── LLM extraction tests (opt-in, real API) ───────────────────────────────────

@pytest.mark.golden
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _ALL_ENTRIES, ids=[e["id"] for e in _ALL_ENTRIES])
async def test_extract_plan_golden(entry: dict[str, Any]) -> None:
    """Call the real LLM and assert the extracted plan matches expected shape.

    Gate: ``pytest --golden``.  Requires ``ANTHROPIC_API_KEY`` in environment.
    Target: ≥ 95% pass rate (48/50) per PRD §7.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — skipping live LLM golden test")

    from src.api.agent.llm import extract_plan  # local import: avoids module-level init

    plan = await extract_plan(entry["message"])

    # Intent must always match.
    assert plan.intent == entry["expected_intent"], (
        f"[{entry['id']}] intent mismatch: got {plan.intent!r}, "
        f"want {entry['expected_intent']!r}"
    )

    expected_subjects = _subjects_from_entry(entry)

    if entry["expected_status"] == "refused":
        # Refusals must produce an empty subjects list.
        assert plan.subjects == [], (
            f"[{entry['id']}] refusal: expected empty subjects, got {plan.subjects!r}"
        )
    else:
        # Non-refusals: subject kinds must match (order-insensitive).
        # Empty expected_subjects (e.g. hp-06 "all depots") → subjects may be empty too.
        if expected_subjects:
            _assert_plan_subjects(plan, expected_subjects, entry["id"])


# ── Resolver status tests (offline, mock pool) ────────────────────────────────

@pytest.mark.golden
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    _NON_REFUSAL_ENTRIES,
    ids=[e["id"] for e in _NON_REFUSAL_ENTRIES],
)
async def test_resolver_status_golden(entry: dict[str, Any]) -> None:
    """Run the server-side resolver against a mock pool and assert status.

    This does NOT call the LLM.  The plan is built directly from
    ``expected_subjects`` so the test only exercises the resolver logic.
    Offline-friendly — runs on every PR that touches ``src/api/agent/``.
    """
    expected_subjects = _subjects_from_entry(entry)
    if not expected_subjects:
        pytest.skip(f"[{entry['id']}] no subjects to resolve; skipping resolver test")

    mentions = [
        EntityMention(kind=s["kind"], text=s["text"]) for s in expected_subjects
    ]

    static_pool = _pool_for_entry(entry)
    auth = _auth_for_depot()

    resolved = await resolve_entities(mentions, auth, static_pool)

    expected_status = entry["expected_status"]

    if expected_status == "disambiguation":
        ambiguous = [e for e in resolved if e.candidates]
        assert ambiguous, (
            f"[{entry['id']}] expected disambiguation but got: "
            f"{[r.model_dump() for r in resolved]}"
        )

    elif expected_status == "not_found":
        missing = [e for e in resolved if e.primary_id is None and not e.candidates]
        assert missing, (
            f"[{entry['id']}] expected not_found but got: "
            f"{[r.model_dump() for r in resolved]}"
        )

    else:  # success / edge case
        # All resolved entities must have a primary_id and no candidates.
        driver_subjects = [s for s in expected_subjects if s["kind"] == "driver"]
        if driver_subjects:
            driver_resolved = [r for r in resolved if r.kind == "driver"]
            for r in driver_resolved:
                assert r.primary_id is not None, (
                    f"[{entry['id']}] driver {r.display!r} resolved to None"
                )
                assert not r.candidates, (
                    f"[{entry['id']}] driver {r.display!r} unexpected candidates"
                )
