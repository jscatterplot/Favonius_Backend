"""Provider contract for the data-source integration framework.

A :class:`DataSourceProvider` knows how to (a) describe the credential form the
frontend renders, (b) validate credentials with a cheap probe, and (c) ingest
the external system's data into the depot. Concrete providers (e.g.
``KempowerProvider``) live alongside this module and register themselves in
``registry.py``.

The catalogue types are Pydantic models so the providers endpoint can serialise
them directly (snake_case, matching the ``/admin`` house style). The runtime
types are dataclasses — they carry live pool handles and are never serialised.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol
from uuid import UUID

import asyncpg
from pydantic import BaseModel, Field

CredentialFieldType = Literal["string", "password", "number", "select"]
IngestionStatus = Literal["succeeded", "partial", "failed"]


class CredentialField(BaseModel):
    """One field in a provider's connect-form, rendered dynamically by the UI."""

    key: str
    label: str
    type: CredentialFieldType = "string"
    required: bool = True
    secret: bool = False
    help_text: Optional[str] = None
    options: Optional[list[dict[str, Any]]] = None
    pattern: Optional[str] = None


class ProviderCatalogueEntry(BaseModel):
    """Catalogue description of a provider returned by ``GET .../providers``."""

    provider_key: str
    display_name: str
    description: str
    credential_fields: list[CredentialField] = Field(default_factory=list)
    supports_scheduled_sync: bool = True
    default_sync_interval_minutes: int = 1440


class JobProgressHandle(Protocol):
    """Callback the runtime hands a provider to report incremental progress."""

    async def update(self, *, stage: Optional[str] = None, **counters: int) -> None:
        """Merge ``stage`` + counter deltas into the job row's progress JSONB."""
        ...


@dataclass
class IngestionContext:
    """Everything a provider needs to run one ingestion against a depot."""

    connection_id: str
    job_id: str
    depot_id: str
    organization_id: str
    # Decrypted secrets, in-memory only. NEVER log or echo these.
    credentials: dict[str, Any]
    # Non-secret connection config (locationId, backfillSince, baseUrl, ...).
    config: dict[str, Any]
    static_pool: asyncpg.Pool
    ts_pool: asyncpg.Pool
    progress: JobProgressHandle
    batch_id: UUID


@dataclass
class IngestionResult:
    """Outcome of one ingestion run, mapped onto the job row's terminal state."""

    status: IngestionStatus
    chargers_created: int = 0
    vehicles_created: int = 0
    sessions_inserted: int = 0
    access_rows_upserted: int = 0
    skipped: list[str] = field(default_factory=list)
    error_detail: Optional[str] = None
    # One-time charger Basic Auth creds surfaced once via the status endpoint;
    # never persisted. Each entry: {"ocpp_id", "username", "password"}.
    credentials_emitted: list[dict[str, str]] = field(default_factory=list)


class DataSourceProvider(abc.ABC):
    """Abstract base every external-system provider implements."""

    @property
    @abc.abstractmethod
    def provider_key(self) -> str:
        """Stable identifier, e.g. ``"kempower"``."""

    @abc.abstractmethod
    def catalogue_entry(self) -> ProviderCatalogueEntry:
        """Return the catalogue/credential-schema description for the UI."""

    @abc.abstractmethod
    async def validate_credentials(
        self, credentials: dict[str, Any], config: dict[str, Any]
    ) -> None:
        """Probe the external system; raise CredentialValidationError if bad."""

    @abc.abstractmethod
    async def run_ingestion(self, ctx: IngestionContext) -> IngestionResult:
        """Pull the external system's data into the depot. Never raise on a
        single bad record — accumulate skips and continue."""
