"""Real-DB assertion that ``agent_views.depots()`` resolves ``entsoe_zone``
from the depot timezone when no explicit override is set
(``migrations/supabase/045_agent_depots_entsoe_zone_fallback.sql``).

This is the SQL-level counterpart to the DB-free drift guard in
``tests/unit/test_agent_depots_entsoe_zone_map.py``: that test proves the
inlined map mirrors the Python source of truth; this one proves the deployed
function actually applies it (override → timezone → NULL), matching
``src/db/queries.py:resolve_bidding_zone``.

Needs only the static (Supabase) test pool — NOT a live Anthropic key — so it
runs whenever the agent-SQL static DB is reachable (CI), and skips locally when
it is not (see ``tests/integration/agent_sql/conftest.py``).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.agent_sql_real, pytest.mark.asyncio]


async def _entsoe_zone_for(conn, *, name: str, timezone: str, tariff_config: dict | None):
    """Insert a site and return the entsoe_zone agent_views.depots() reports."""
    site_id = uuid4()
    await conn.execute(
        "INSERT INTO public.sites (id, name, timezone, tariff_config) "
        "VALUES ($1, $2, $3, $4::jsonb)",
        site_id,
        name,
        timezone,
        json.dumps(tariff_config) if tariff_config is not None else None,
    )
    row = await conn.fetchrow(
        "SELECT entsoe_zone FROM agent_views.depots($1::uuid[])",
        [site_id],
    )
    assert row is not None, "depots() returned no row for the inserted site"
    return row["entsoe_zone"]


async def test_depots_entsoe_zone_resolution(sql_real_static_pool):
    """Override → timezone-derived → NULL, all in one rolled-back transaction."""
    async with sql_real_static_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            # No override → derived from the Europe/Vilnius timezone (the live pilot case).
            assert (
                await _entsoe_zone_for(
                    conn, name="Vilnius fallback", timezone="Europe/Vilnius", tariff_config=None
                )
                == "10YLT-1001A0008Q"
            )

            # Explicit operator override wins over the timezone derivation.
            assert (
                await _entsoe_zone_for(
                    conn,
                    name="Override depot",
                    timezone="Europe/Vilnius",
                    tariff_config={"entsoe_zone": "10Y1001A1001A82H"},
                )
                == "10Y1001A1001A82H"
            )

            # Empty-string override is treated as unset, then falls back to timezone.
            assert (
                await _entsoe_zone_for(
                    conn,
                    name="Empty override depot",
                    timezone="Europe/Vilnius",
                    tariff_config={"entsoe_zone": ""},
                )
                == "10YLT-1001A0008Q"
            )

            # Padded override is trimmed (mirrors resolve_bidding_zone's .strip()),
            # so the exact-match join against prices_hourly.bidding_zone still works.
            assert (
                await _entsoe_zone_for(
                    conn,
                    name="Padded override depot",
                    timezone="Europe/Vilnius",
                    tariff_config={"entsoe_zone": "  10Y1001A1001A82H  "},
                )
                == "10Y1001A1001A82H"
            )

            # Whitespace-only override is treated as unset, then falls back to timezone.
            assert (
                await _entsoe_zone_for(
                    conn,
                    name="Whitespace override depot",
                    timezone="Europe/Vilnius",
                    tariff_config={"entsoe_zone": "   "},
                )
                == "10YLT-1001A0008Q"
            )

            # Unmapped (non-European) timezone, no override → NULL (not a fabricated zone).
            assert (
                await _entsoe_zone_for(
                    conn, name="NY depot", timezone="America/New_York", tariff_config=None
                )
                is None
            )
        finally:
            await tx.rollback()
