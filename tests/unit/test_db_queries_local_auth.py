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
    the pilot depot.
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


@pytest.mark.asyncio
async def test_list_authorized_id_tags_includes_orphan_cards() -> None:
    """Active RFID cards must be pushed regardless of vehicle/driver
    assignment, matching the central ``resolve_id_tag_identity`` path.

    Earlier iterations of this query gated cards on ``rfid_card_vehicle_assignments``
    / ``rfid_card_driver_assignments`` EXISTS clauses, which silently
    excluded operator deployments that use cards without populating those
    linkage tables (the pilot depot: 15 active orphan cards, zero vehicles).
    That broke the invariant 'anything that authorizes online also
    authorizes offline' and produced empty SendLocalList pushes.
    """
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])

    await list_authorized_id_tags(db, "station-001")

    sql = " ".join(db.fetch.await_args.args[0].split())

    # The card branch must NOT require an assignment EXISTS.
    assert "rfid_card_vehicle_assignments" not in sql, (
        "Card inclusion must not depend on vehicle assignment — orphan "
        "cards still authorize online and so must authorize offline."
    )
    assert "rfid_card_driver_assignments" not in sql, (
        "Card inclusion must not depend on driver assignment — orphan "
        "cards still authorize online and so must authorize offline."
    )
    # And the card branch itself must be present.
    assert "FROM rfid_cards c" in sql
    assert "c.status = 'active'" in sql
