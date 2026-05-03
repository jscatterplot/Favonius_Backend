"""Entity and time-window resolution for the depot chat agent.

This is the single architectural layer where ``EntityMention`` text
becomes a real UUID and a ``TimeWindow`` phrase becomes real UTC bounds.
The LLM never sees IDs, and IDs never originate from LLM output — every
UUID returned here came from a Supabase row that the caller's
``visible_depot_ids`` already authorized.

The Supabase column conventions (``id`` / ``site_id`` rather than
``driver_id`` / ``depot_id``) are aliased back to the backend's vocabulary
at the SQL boundary, matching the project-wide pattern documented in
``CLAUDE.md``. The ``drivers`` table has no ``organization_id`` column;
tenant scoping for drivers therefore JOINs through ``sites``. Vehicles
do carry ``organization_id`` natively, so the vehicle resolver uses it
directly when the caller is a tenant user.

Reference:
- ``docs/plans/agent_search_architecture_v0.md`` §§ 3.2, 3.4, 3.5.
- Live Supabase schema verified 2026-05-03 against project
  ``favonius-pilot`` (``hmxdpuzqkotmorheyexv``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from src.api.agent.auth_context import AuthContext
from src.api.agent.plan import EntityMention, TimeWindow

EntityKind = Literal["driver", "vehicle", "depot", "rfid"]


class ResolvedEntity(BaseModel):
    """A subject mention, resolved (or not) to a static-DB row.

    ``primary_id=None`` is the "not found" signal. ``candidates`` is
    populated only when the underlying lookup matched more than one
    row; the head of the list is also the primary entity, so the
    caller can render disambiguation UX without losing the top match.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    display: str
    primary_id: Optional[UUID] = None
    card_ids: list[UUID] = Field(default_factory=list)
    candidates: list["ResolvedEntity"] = Field(default_factory=list)


ResolvedEntity.model_rebuild()


class ResolvedTimeWindow(BaseModel):
    """A half-open UTC interval ``[start_utc, end_utc)`` plus its source TZ.

    The ``timezone`` field carries the IANA name used to anchor the
    interval, so the compiler can pass it through to ``AT TIME ZONE``
    when grouping by local day / month.
    """

    model_config = ConfigDict(extra="forbid")

    start_utc: datetime
    end_utc: datetime
    timezone: str


_ResolverFn = Callable[[str, AuthContext, Any], Awaitable[list[ResolvedEntity]]]


# --------------------------------------------------------------------------- #
# Per-kind resolvers
# --------------------------------------------------------------------------- #


async def _resolve_driver(text: str, auth: AuthContext, static_pool: Any) -> list[ResolvedEntity]:
    """Resolve a driver mention via Supabase.

    The ``drivers`` table has no ``organization_id`` column, so org
    scoping JOINs through ``sites`` and filters by
    ``auth.visible_depot_ids``. RFID card assignments are aggregated in
    the same query so the compiler can OR ``card_id`` into the
    ``charging_sessions`` filter without a second round-trip.
    """
    rows = await static_pool.fetch(
        """
        SELECT
            d.id            AS driver_id,
            d.display_name,
            d.site_id       AS depot_id,
            d.external_driver_id,
            s.name          AS depot_name,
            COALESCE(
                array_agg(rcda.card_id) FILTER (WHERE rcda.card_id IS NOT NULL),
                ARRAY[]::uuid[]
            ) AS card_ids
        FROM drivers d
        JOIN sites s ON s.id = d.site_id
        LEFT JOIN rfid_card_driver_assignments rcda ON rcda.driver_id = d.id
        WHERE d.site_id = ANY($1::uuid[])
          AND d.status = 'active'
          AND (
            d.display_name      ILIKE $2
            OR d.email          ILIKE $2
            OR d.external_driver_id ILIKE $2
          )
        GROUP BY d.id, s.name
        ORDER BY d.display_name
        LIMIT 10
        """,
        auth.visible_depot_ids,
        f"%{text}%",
    )

    if not rows:
        return [ResolvedEntity(kind="driver", display=text, primary_id=None)]

    candidates = [
        ResolvedEntity(
            kind="driver",
            display=f"{row['display_name']} ({row['depot_name']})",
            primary_id=_as_uuid(row["driver_id"]),
            card_ids=[_as_uuid(c) for c in (row["card_ids"] or [])],
        )
        for row in rows
    ]
    return _wrap_candidates(candidates)


async def _resolve_vehicle(text: str, auth: AuthContext, static_pool: Any) -> list[ResolvedEntity]:
    """Resolve a vehicle mention via Supabase.

    Vehicles carry ``organization_id`` natively, so for a tenant caller
    we filter by ``v.organization_id`` and additionally constrain to
    visible depots (defence-in-depth). For ``favonius_admin`` (no
    organization_id), we fall back to the visible-depot scope, which
    spans every depot in the system.
    """
    if auth.organization_id is None:
        rows = await static_pool.fetch(
            """
            SELECT
                v.id   AS vehicle_id,
                COALESCE(v.display_name, v.license_plate, v.vin, v.external_id)
                       AS display_text,
                v.site_id AS depot_id,
                s.name    AS depot_name
            FROM vehicles v
            JOIN sites s ON s.id = v.site_id
            WHERE v.site_id = ANY($1::uuid[])
              AND (
                v.display_name   ILIKE $2
                OR v.vin         ILIKE $2
                OR v.external_id ILIKE $2
                OR v.license_plate ILIKE $2
              )
            ORDER BY display_text
            LIMIT 10
            """,
            auth.visible_depot_ids,
            f"%{text}%",
        )
    else:
        rows = await static_pool.fetch(
            """
            SELECT
                v.id   AS vehicle_id,
                COALESCE(v.display_name, v.license_plate, v.vin, v.external_id)
                       AS display_text,
                v.site_id AS depot_id,
                s.name    AS depot_name
            FROM vehicles v
            JOIN sites s ON s.id = v.site_id
            WHERE v.organization_id = $1::uuid
              AND v.site_id = ANY($2::uuid[])
              AND (
                v.display_name   ILIKE $3
                OR v.vin         ILIKE $3
                OR v.external_id ILIKE $3
                OR v.license_plate ILIKE $3
              )
            ORDER BY display_text
            LIMIT 10
            """,
            str(auth.organization_id),
            auth.visible_depot_ids,
            f"%{text}%",
        )

    if not rows:
        return [ResolvedEntity(kind="vehicle", display=text, primary_id=None)]

    candidates = [
        ResolvedEntity(
            kind="vehicle",
            display=f"{row['display_text']} ({row['depot_name']})",
            primary_id=_as_uuid(row["vehicle_id"]),
        )
        for row in rows
    ]
    return _wrap_candidates(candidates)


async def _resolve_depot(text: str, auth: AuthContext, static_pool: Any) -> list[ResolvedEntity]:
    """Resolve a depot mention via Supabase.

    Filters by ``id = ANY(visible_depot_ids)`` so a name match against
    a depot in another tenant's organization returns zero rows.
    """
    rows = await static_pool.fetch(
        """
        SELECT
            s.id   AS depot_id,
            s.name AS depot_name
        FROM sites s
        WHERE s.id = ANY($1::uuid[])
          AND s.name ILIKE $2
        ORDER BY s.name
        LIMIT 10
        """,
        auth.visible_depot_ids,
        f"%{text}%",
    )

    if not rows:
        return [ResolvedEntity(kind="depot", display=text, primary_id=None)]

    candidates = [
        ResolvedEntity(
            kind="depot",
            display=row["depot_name"],
            primary_id=_as_uuid(row["depot_id"]),
        )
        for row in rows
    ]
    return _wrap_candidates(candidates)


async def _resolve_rfid(text: str, auth: AuthContext, static_pool: Any) -> list[ResolvedEntity]:
    """Resolve an RFID mention via Supabase.

    ``id_tag`` is matched exactly (it's the OCPP-side identity and
    operators usually paste it verbatim). ``label`` is matched ILIKE
    for the human-readable case ("delivery card 3"). Tenant scoping
    JOINs through ``sites``.
    """
    rows = await static_pool.fetch(
        """
        SELECT
            r.id      AS card_id,
            r.id_tag,
            r.label,
            r.site_id AS depot_id,
            s.name    AS depot_name
        FROM rfid_cards r
        JOIN sites s ON s.id = r.site_id
        WHERE r.site_id = ANY($1::uuid[])
          AND (
            r.id_tag = $2
            OR r.label ILIKE $3
          )
        ORDER BY r.id_tag
        LIMIT 10
        """,
        auth.visible_depot_ids,
        text,
        f"%{text}%",
    )

    if not rows:
        return [ResolvedEntity(kind="rfid", display=text, primary_id=None)]

    candidates = [
        ResolvedEntity(
            kind="rfid",
            display=f"{row['label'] or row['id_tag']} ({row['depot_name']})",
            primary_id=_as_uuid(row["card_id"]),
        )
        for row in rows
    ]
    return _wrap_candidates(candidates)


_RESOLVERS: dict[str, _ResolverFn] = {
    "driver": _resolve_driver,
    "vehicle": _resolve_vehicle,
    "depot": _resolve_depot,
    "rfid": _resolve_rfid,
}


async def resolve_entities(
    mentions: list[EntityMention],
    auth: AuthContext,
    static_pool: Any,
) -> list[ResolvedEntity]:
    """Resolve a list of ``EntityMention`` to ``ResolvedEntity``.

    Dispatches each mention to its per-kind resolver and concatenates
    the results. Ambiguous matches return a single head ``ResolvedEntity``
    whose ``candidates`` list carries the alternatives; not-found
    matches return a single ``ResolvedEntity`` with ``primary_id=None``.
    The shape is stable so the caller can detect both states with
    simple comprehensions.
    """
    if not mentions:
        return []
    resolved: list[ResolvedEntity] = []
    for mention in mentions:
        resolver = _RESOLVERS[mention.kind]
        resolved.extend(await resolver(mention.text, auth, static_pool))
    return resolved


# --------------------------------------------------------------------------- #
# Time window resolver
# --------------------------------------------------------------------------- #


def resolve_time_window(
    window: TimeWindow,
    visible_depot_ids: list[UUID],
    depot_timezones: dict[UUID, str],
    *,
    now: Optional[datetime] = None,
) -> ResolvedTimeWindow:
    """Resolve a relative phrase or absolute date pair to UTC bounds.

    The interval is anchored in the depot's local timezone, so a
    "yesterday" question on a Vilnius depot means *Vilnius* yesterday,
    not UTC yesterday — matching accounting intuition. UTC bounds are
    computed by ``ZoneInfo`` so DST transitions are handled correctly:
    a "last_week" call spanning the spring-forward weekend yields a
    UTC interval one hour shorter than 7×24h, and a fall-back week is
    one hour longer.

    **Multi-timezone contract.** When the caller's visible depots span
    more than one IANA timezone, this function raises ``ValueError``.
    The compiler is then responsible for grouping subjects by timezone
    and calling this function once per group. This keeps a single
    ``ResolvedTimeWindow`` unambiguously associated with a single TZ
    (the SQL needs that to drive ``AT TIME ZONE`` correctly).

    Args:
        window: The :class:`TimeWindow` extracted from the LLM plan.
        visible_depot_ids: The caller's authorized depot scope; used
            here only to filter ``depot_timezones``.
        depot_timezones: Map of depot UUID → IANA timezone string, as
            returned by :func:`load_depot_timezones`.
        now: Optional override for "current time" (UTC-aware), used
            by tests to fix the relative anchor. Defaults to
            ``datetime.now(timezone.utc)`` when unset.

    Returns:
        A :class:`ResolvedTimeWindow` with UTC start (inclusive) and
        end (exclusive) datetimes and the IANA timezone used.

    Raises:
        ValueError: If no timezone is available, if the visible depots
            span multiple timezones, or if the ``TimeWindow`` is
            structurally invalid in a way Pydantic missed.
    """
    tzs = {depot_timezones[d] for d in visible_depot_ids if d in depot_timezones}
    if not tzs:
        raise ValueError(
            "resolve_time_window: no depot timezones available for the visible depot list"
        )
    if len(tzs) > 1:
        raise ValueError(
            "resolve_time_window: subjects span multiple timezones "
            f"{sorted(tzs)}; call this function once per timezone group"
        )
    tz_name = next(iter(tzs))
    tz = ZoneInfo(tz_name)

    if window.kind == "relative":
        # Pydantic guarantees ``relative`` is set when kind='relative',
        # but the explicit check keeps mypy + future readers honest.
        if window.relative is None:  # pragma: no cover - validator already gates
            raise ValueError("TimeWindow(kind='relative') missing 'relative' field")
        start_local, end_local = _resolve_relative(window.relative, tz, now=now)
    else:
        if window.from_iso is None or window.to_iso is None:  # pragma: no cover
            raise ValueError("TimeWindow(kind='absolute') requires 'from_iso' and 'to_iso'")
        from_d = date.fromisoformat(window.from_iso)
        to_d = date.fromisoformat(window.to_iso)
        start_local = _start_of_day(from_d, tz)
        end_local = _start_of_day(to_d, tz)

    return ResolvedTimeWindow(
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        timezone=tz_name,
    )


def _resolve_relative(
    rel: str, tz: ZoneInfo, *, now: Optional[datetime]
) -> tuple[datetime, datetime]:
    """Resolve a single relative literal to a (start_local, end_local) pair."""
    if now is None:
        now_local = datetime.now(tz=tz)
    else:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now_local = now.astimezone(tz)
    today = now_local.date()

    if rel == "today":
        return _start_of_day(today, tz), _start_of_day(today + timedelta(days=1), tz)

    if rel == "yesterday":
        return _start_of_day(today - timedelta(days=1), tz), _start_of_day(today, tz)

    if rel == "this_week":
        monday = today - timedelta(days=today.weekday())
        return (
            _start_of_day(monday, tz),
            _start_of_day(monday + timedelta(days=7), tz),
        )

    if rel == "last_week":
        monday = today - timedelta(days=today.weekday())
        last_monday = monday - timedelta(days=7)
        return _start_of_day(last_monday, tz), _start_of_day(monday, tz)

    if rel == "this_month":
        first = date(today.year, today.month, 1)
        return _start_of_day(first, tz), _start_of_day(_next_month(first), tz)

    if rel == "last_month":
        first_this = date(today.year, today.month, 1)
        last_first = _previous_month(first_this)
        return _start_of_day(last_first, tz), _start_of_day(first_this, tz)

    raise ValueError(f"unknown relative literal: {rel!r}")


def _start_of_day(d: date, tz: ZoneInfo) -> datetime:
    """Construct midnight at ``tz`` for date ``d``."""
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _next_month(first: date) -> date:
    """Return the first day of the month after ``first``."""
    if first.month == 12:
        return date(first.year + 1, 1, 1)
    return date(first.year, first.month + 1, 1)


def _previous_month(first: date) -> date:
    """Return the first day of the month before ``first``."""
    if first.month == 1:
        return date(first.year - 1, 12, 1)
    return date(first.year, first.month - 1, 1)


# --------------------------------------------------------------------------- #
# Depot-timezone helper (cached per process)
# --------------------------------------------------------------------------- #

_TZ_CACHE: dict[frozenset[UUID], dict[UUID, str]] = {}


async def load_depot_timezones(
    static_pool: Any,
    visible_depot_ids: list[UUID],
) -> dict[UUID, str]:
    """Pull ``id, timezone`` for the given depot UUIDs.

    Cached in-process by the frozenset of UUIDs; the cache is intended
    to amortize within a single agent turn (avoid a second round-trip
    when the resolver and the time-window step both want timezones)
    rather than to live forever — call :func:`_clear_tz_cache` from
    tests to reset.
    """
    if not visible_depot_ids:
        return {}
    key = frozenset(visible_depot_ids)
    cached = _TZ_CACHE.get(key)
    if cached is not None:
        return cached

    rows = await static_pool.fetch(
        """
        SELECT id, timezone
        FROM sites
        WHERE id = ANY($1::uuid[])
        """,
        visible_depot_ids,
    )
    result: dict[UUID, str] = {}
    for row in rows:
        tz = row["timezone"]
        if tz:
            result[_as_uuid(row["id"])] = tz
    _TZ_CACHE[key] = result
    return result


def _clear_tz_cache() -> None:
    """Reset the in-process timezone cache. Used by tests."""
    _TZ_CACHE.clear()


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _as_uuid(value: Any) -> UUID:
    """Coerce an asyncpg-returned value to ``UUID``.

    asyncpg yields native ``UUID`` objects for ``uuid`` columns, but
    tests mocking the pool with plain dicts may pass strings. Both
    paths land here.
    """
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _wrap_candidates(candidates: list[ResolvedEntity]) -> list[ResolvedEntity]:
    """Collapse a candidate list to a single head when ambiguous.

    The caller's contract is "one ``ResolvedEntity`` per mention". When
    multiple rows match, we return the top match as the primary and
    attach the full list (head + tail) on its ``candidates`` field so
    the disambiguation UI has the materials to ask the user.
    """
    if len(candidates) == 1:
        return candidates
    head = candidates[0].model_copy(update={"candidates": list(candidates)})
    return [head]
