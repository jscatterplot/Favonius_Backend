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
