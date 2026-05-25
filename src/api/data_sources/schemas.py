"""Request models for the Data Sources endpoints.

Request bodies accept camelCase or snake_case (``populate_by_name=True`` +
``alias_generator=to_camel``). Responses are emitted snake_case (the ``/admin``
house style — see ``tests/unit/test_api_wire_format.py``) and are built as plain
dicts from asyncpg records in the router.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class CreateConnectionRequest(_Base):
    """Body for ``POST /admin/data-sources/connections``."""

    depot_id: str
    provider_key: str
    display_name: Optional[str] = None
    sync_interval_minutes: Optional[int] = None
    scheduled_sync_enabled: bool = True
    credentials: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateConnectionRequest(_Base):
    """Body for ``PATCH /admin/data-sources/connections/{id}``."""

    display_name: Optional[str] = None
    sync_interval_minutes: Optional[int] = None
    scheduled_sync_enabled: Optional[bool] = None
    status: Optional[str] = None
    credentials: Optional[dict[str, Any]] = None
