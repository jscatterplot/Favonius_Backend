from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.queries import list_authorized_id_tags


@pytest.mark.asyncio
async def test_list_authorized_id_tags_deduplicates_on_id_tag_in_sql() -> None:
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])

    await list_authorized_id_tags(db, "station-001")

    executed_sql = db.fetch.await_args.args[0]
    assert "SELECT DISTINCT ON (id_tag) id_tag, source" in executed_sql
    assert "ORDER BY id_tag, source_priority" in executed_sql


@pytest.mark.asyncio
async def test_list_authorized_id_tags_compares_access_default_as_string() -> None:
    """``sites.charger_vehicle_access_default`` is a varchar enum
    (``'all_to_all' | 'explicit_matrix'``), not a boolean.

    Comparing it to ``TRUE`` raises
    ``operator does not exist: character varying = boolean`` at runtime,
    so the WHERE clause must use the canonical string literal instead.
    Regression guard for the offline-RFID sync that crashed against the
    HRX Vilnius pilot.
    """
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])

    await list_authorized_id_tags(db, "station-001")

    sql = db.fetch.await_args.args[0]
    # Normalise whitespace so this isn't fragile to indentation tweaks.
    compact = " ".join(sql.split())
    assert "si.access_default = 'all_to_all'" in compact, (
        "access_default must be compared against the 'all_to_all' string "
        "literal, not TRUE — the column is varchar in production."
    )
    assert "si.access_default = TRUE" not in compact, (
        "Found legacy boolean comparison; PG raises a type error against "
        "the actual schema."
    )
