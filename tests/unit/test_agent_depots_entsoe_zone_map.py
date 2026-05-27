"""Drift guard: the timezone -> bidding-zone map inlined into
``migrations/supabase/045_agent_depots_entsoe_zone_fallback.sql`` must stay a
verbatim mirror of ``src/adapters/entsoe/mappings.py:TIMEZONE_TO_BIDDING_ZONE``.

The agent's ``agent_views.depots()`` resolves ``entsoe_zone`` from the depot
timezone when no explicit override is set, mirroring the Python
``resolve_bidding_zone`` cascade. That fallback is necessarily duplicated in
SQL (the view is pure SQL); this test makes the duplication safe by failing
loudly if the two maps diverge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.adapters.entsoe.mappings import TIMEZONE_TO_BIDDING_ZONE

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "supabase"
    / "045_agent_depots_entsoe_zone_fallback.sql"
)

_BLOCK_RE = re.compile(
    r">>> ENTSOE_TZ_ZONE_MAP.*?>>>(?P<body>.*?)<<< ENTSOE_TZ_ZONE_MAP",
    re.DOTALL,
)
_PAIR_RE = re.compile(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")


def _parse_sql_map() -> dict[str, str]:
    sql = _MIGRATION.read_text(encoding="utf-8")
    block = _BLOCK_RE.search(sql)
    assert block is not None, "ENTSOE_TZ_ZONE_MAP sentinels not found in migration 045"
    pairs = _PAIR_RE.findall(block.group("body"))
    assert pairs, "no (tz, zone) pairs parsed from the migration map"
    mapping: dict[str, str] = {}
    for tz, zone in pairs:
        assert tz not in mapping, f"duplicate timezone {tz!r} in migration map"
        mapping[tz] = zone
    return mapping


def test_migration_map_matches_python_source_of_truth():
    sql_map = _parse_sql_map()
    assert sql_map == TIMEZONE_TO_BIDDING_ZONE, (
        "migration 045 timezone->zone map drifted from "
        "src/adapters/entsoe/mappings.py:TIMEZONE_TO_BIDDING_ZONE. "
        f"only-in-SQL={set(sql_map) - set(TIMEZONE_TO_BIDDING_ZONE)}, "
        f"only-in-Python={set(TIMEZONE_TO_BIDDING_ZONE) - set(sql_map)}, "
        "value-mismatches="
        f"{{tz for tz in sql_map.keys() & TIMEZONE_TO_BIDDING_ZONE.keys() if sql_map[tz] != TIMEZONE_TO_BIDDING_ZONE[tz]}}"
    )


@pytest.mark.parametrize(
    "timezone,expected_zone",
    [
        ("Europe/Vilnius", "10YLT-1001A0008Q"),  # the depots in the live pilot
        ("Europe/Berlin", "10Y1001A1001A82H"),
    ],
)
def test_migration_map_pins_known_zones(timezone, expected_zone):
    assert _parse_sql_map()[timezone] == expected_zone
