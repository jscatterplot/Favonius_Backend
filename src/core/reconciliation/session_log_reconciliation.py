"""Reconcile a charger's own session log against our recorded data.

Mirrors the diagnostic style of :mod:`src.core.billing.session_cost`:

* A dataclass result with an explicit ``source`` field — readers and
  tests gate on the string, not on numeric thresholds.
* Idempotent upsert via :func:`write_session_log_reconciliation` so
  the reconciler can be re-run safely (a retry after a transient DB
  blip overwrites the prior row rather than appending).
* No silent failures: every unreachable-looking case maps to one of
  the documented ``source`` values.

Energy on our side is computed via trapezoidal integration of
``telemetry.charging_kw``, the same shape the cost calculator uses. The
calculator's SQL lives in :mod:`src.core.billing.session_cost`; for
the reconciler we want a single scalar (total kWh + observed seconds
over the session window), so the SQL here is a cut-down version of
the same idea rather than a shared helper. Centralising would be
welcome but the two queries diverge enough (the cost calc groups by
hour bucket for price joining, the reconciler doesn't) that a
premature abstraction would be more confusing than helpful.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


ReconciliationSource = Literal[
    "reconciled",
    "partial",
    "no_log_entries",
    "no_session",
    "parse_failed",
]


@dataclass(frozen=True)
class ReconciliationResult:
    """Outcome of one reconciliation pass.

    ``source`` is the persisted provenance string. The numeric fields
    are best-effort and may be ``None`` when the corresponding side
    did not yield usable data; the source enum disambiguates.

    ``energy_delta_pct`` is ``(charger - ours) / ours`` as a fraction
    (positive when the charger reported more than we did). Tests gate
    on this in addition to ``source`` to catch silent unit-conversion
    regressions.
    """

    source: ReconciliationSource
    our_energy_kwh: Optional[float] = None
    charger_energy_kwh: Optional[float] = None
    energy_delta_pct: Optional[float] = None
    our_duration_s: Optional[int] = None
    charger_duration_s: Optional[int] = None
    our_start_time: Optional[datetime] = None
    charger_start_time: Optional[datetime] = None
    our_end_time: Optional[datetime] = None
    charger_end_time: Optional[datetime] = None
    notes: dict[str, Any] = field(default_factory=dict)


async def reconcile_session_log(
    ts_pool: asyncpg.Pool,
    *,
    session_id: UUID,
    log_import_id: UUID,
) -> ReconciliationResult:
    """Compute deltas between our recorded session and the charger's log.

    Reads:
      * ``charging_sessions`` row for ``session_id``
      * ``charger_log_imports`` row for ``log_import_id`` (status check)
      * ``charger_session_log_entries`` rows for that import
      * ``telemetry`` over the session window (our-side energy)

    Returns a :class:`ReconciliationResult`. Never raises on shape —
    every unhealthy state maps to a ``source`` value. ``parse_failed``
    is reserved for the upstream parser; this function emits it only
    when the import row itself is marked ``failed``.
    """
    async with ts_pool.acquire() as conn:
        session = await conn.fetchrow(
            """
            SELECT session_id, station_id, connector_id, transaction_id,
                   vehicle_id, start_time, end_time, energy_delivered_kwh
              FROM charging_sessions
             WHERE session_id = $1
            """,
            session_id,
        )
        if session is None:
            return ReconciliationResult(source="no_session")

        import_row = await conn.fetchrow(
            """
            SELECT id, status, error_message
              FROM charger_log_imports
             WHERE id = $1
            """,
            log_import_id,
        )
        if import_row is None:
            # The caller should never invoke us without a real import,
            # but a deleted/expired row is treated like a parse failure
            # so the UI surface still gets a row to display.
            return ReconciliationResult(source="parse_failed")
        if import_row["status"] == "failed":
            return ReconciliationResult(
                source="parse_failed",
                notes={"import_error": import_row["error_message"]},
            )

        # Charger-side: aggregate the parsed entries scoped to this import
        # AND this transaction (defence-in-depth — the parser shouldn't
        # leak cross-transaction rows, but if it does, the join keeps us
        # honest).
        charger_row = await conn.fetchrow(
            _CHARGER_AGGREGATE_SQL,
            log_import_id,
            session["station_id"],
            session["transaction_id"],
        )

    charger_entries = int(charger_row["sample_count"] or 0) if charger_row else 0
    if charger_entries == 0:
        return ReconciliationResult(source="no_log_entries")

    # Our side: integrate charging_kw over the session window.
    our_energy = await _our_energy_kwh(
        ts_pool,
        station_id=session["station_id"],
        connector_id=session["connector_id"],
        transaction_id=session["transaction_id"],
        start_time=session["start_time"],
        end_time=session["end_time"],
    )

    charger_energy = _charger_total_kwh(
        max_energy_kwh=_as_float(charger_row["max_energy_kwh"]),
        min_energy_kwh=_as_float(charger_row["min_energy_kwh"]),
    )

    delta_pct: Optional[float]
    if our_energy is not None and our_energy > 0 and charger_energy is not None:
        delta_pct = (charger_energy - our_energy) / our_energy
    else:
        delta_pct = None

    our_start = session["start_time"]
    our_end = session["end_time"]
    charger_start = charger_row["min_time"]
    charger_end = charger_row["max_time"]

    our_duration = _duration_s(our_start, our_end)
    charger_duration = _duration_s(charger_start, charger_end)

    have_both_sides = (
        our_energy is not None
        and our_energy > 0
        and charger_energy is not None
        and charger_energy > 0
    )
    source: ReconciliationSource = "reconciled" if have_both_sides else "partial"

    notes: dict[str, Any] = {
        "charger_entries": charger_entries,
        "energy_delivered_kwh_recorded": _as_float(session["energy_delivered_kwh"]),
    }

    return ReconciliationResult(
        source=source,
        our_energy_kwh=our_energy,
        charger_energy_kwh=charger_energy,
        energy_delta_pct=delta_pct,
        our_duration_s=our_duration,
        charger_duration_s=charger_duration,
        our_start_time=our_start,
        charger_start_time=charger_start,
        our_end_time=our_end,
        charger_end_time=charger_end,
        notes=notes,
    )


_CHARGER_AGGREGATE_SQL = """
    SELECT COUNT(*)            AS sample_count,
           MIN(time)           AS min_time,
           MAX(time)           AS max_time,
           MAX(energy_kwh)     AS max_energy_kwh,
           MIN(energy_kwh) FILTER (WHERE energy_kwh IS NOT NULL)
                               AS min_energy_kwh
      FROM charger_session_log_entries
     WHERE log_import_id = $1
       AND ($2::text IS NULL OR station_id = $2)
       -- When a concrete transaction_id is known, scope strictly to it
       -- so multi-session diagnostic dumps can't bleed rows from other
       -- transactions into this session's aggregate. When transaction_id
       -- is unknown (legacy imports), fall back to "anything in the
       -- import for this station".
       AND ($3::bigint IS NULL OR transaction_id = $3)
"""


def _charger_total_kwh(
    *,
    max_energy_kwh: Optional[float],
    min_energy_kwh: Optional[float],
) -> Optional[float]:
    """Total energy from cumulative meter readings.

    ABB Terra AC and other OCPP-compliant chargers report
    ``energy_kwh`` as the cumulative meter reading at each sample, so
    the session total is ``max - min``. The two ambiguous cases:

      * **All samples report the same cumulative meter value.**
        The vehicle was plugged in but idle. Returning ``max`` here
        would conflate that with "5000 kWh delivered" when the meter
        sat at 5000 kWh the whole time — clearly wrong. Return ``0.0``.
      * **Only one sample present** (no min/max spread). We can't
        integrate over the session window from a single point; the
        honest answer is also ``0.0`` — the reconciler will surface
        ``source='partial'`` against any non-zero telemetry side.

    Returns:
        ``None`` when the parser yielded no usable energy data at all
        (max is None or non-positive).
        ``0.0`` when ``max - min`` is non-positive (idle / single-sample).
        ``max - min`` otherwise.
    """
    if max_energy_kwh is None or max_energy_kwh <= 0:
        return None
    if min_energy_kwh is None:
        # Some parsers (e.g. delta-style emitters; future-only path)
        # don't populate ``min_energy_kwh`` because each sample is
        # already a per-interval delta. ``max`` is the appropriate
        # total in that case.
        return max_energy_kwh
    delta = max_energy_kwh - min_energy_kwh
    if delta <= 0:
        return 0.0
    return delta


# How far forward the final MeterValues sample is allowed to be
# extrapolated when integrating power × time. OCPP's
# ``MeterValueSampleInterval`` is typically 30-60 s; capping at 5
# minutes admits one stalled sample per session without letting a
# stale final reading dominate the integral when the charger drops
# its socket long before the StopTransaction message lands. Matches
# the spirit of ``MAX_TELEMETRY_AGE`` (15 min) used elsewhere as the
# staleness threshold, scaled down to a single sample's worth of
# tail.
_TAIL_EXTRAPOLATION_CAP_SECONDS = 300


_OUR_ENERGY_SQL = """
        WITH samples AS (
            SELECT t.time AS raw_time,
                   t.charging_kw,
                   LEAD(t.time) OVER (ORDER BY t.time) AS next_time,
                   LEAD(t.charging_kw) OVER (ORDER BY t.time) AS next_kw
              FROM telemetry t
             WHERE t.station_id = $1
               AND t.connector_id = $2
               {tx_filter}
               AND t.time >= $4::timestamptz - INTERVAL '15 minutes'
               AND t.time < $5::timestamptz
               AND t.charging_kw IS NOT NULL
               AND t.charging_kw > 0
        )
        SELECT
            COALESCE(
                SUM(
                    (charging_kw + COALESCE(next_kw, charging_kw)) / 2.0
                    * EXTRACT(EPOCH FROM (LEAST(next_time, $5::timestamptz) - GREATEST(raw_time, $4::timestamptz))) / 3600.0
                ) FILTER (WHERE next_time IS NOT NULL),
                0
            ) + COALESCE(
                SUM(
                    -- Tail term: extrapolate the last observed power
                    -- forward to ``end_time``, but cap the tail at
                    -- ``_TAIL_EXTRAPOLATION_CAP_S`` seconds so a stale
                    -- final sample doesn't get integrated across the
                    -- whole rest of the session window. Cursor Medium
                    -- flagged this as a double-count risk for sessions
                    -- whose last MeterValues arrived long before
                    -- StopTransaction — a real concern for
                    -- chargers that drop their socket toward
                    -- session end. The cap matches typical OCPP
                    -- MeterValueSampleInterval bounds.
                    charging_kw
                    * LEAST(
                        EXTRACT(EPOCH FROM ($5::timestamptz - GREATEST(raw_time, $4::timestamptz))),
                        {tail_cap_s}
                      ) / 3600.0
                ) FILTER (WHERE next_time IS NULL),
                0
            ) AS energy_kwh
        FROM samples
        WHERE next_time IS NULL OR next_time > $4::timestamptz
"""


async def _our_energy_kwh(
    ts_pool: asyncpg.Pool,
    *,
    station_id: Optional[str],
    connector_id: Optional[int],
    transaction_id: Optional[int],
    start_time: Optional[datetime],
    end_time: Optional[datetime],
) -> Optional[float]:
    """Trapezoidal integration of telemetry.charging_kw across the window.

    Cascade when ``transaction_id`` is set: strict ``transaction_id = $3``
    first; if that yields no positive energy, retry with
    ``transaction_id IS NULL OR transaction_id = $3`` so legacy untagged
    samples still count without mixing in other sessions' rows. Same
    philosophy as :func:`src.core.billing.session_cost._fetch_granular_telemetry_rows`.
    """
    if start_time is None or end_time is None:
        return None
    if station_id is None or connector_id is None:
        return None

    async def _query(conn: asyncpg.Connection, tx_filter: str) -> Optional[float]:
        row = await conn.fetchrow(
            _OUR_ENERGY_SQL.format(
                tx_filter=tx_filter,
                tail_cap_s=_TAIL_EXTRAPOLATION_CAP_SECONDS,
            ),
            station_id,
            connector_id,
            transaction_id,
            start_time,
            end_time,
        )
        if row is None:
            return None
        value = _as_float(row["energy_kwh"])
        if value is None or value <= 0:
            return None
        return value

    async with ts_pool.acquire() as conn:
        if transaction_id is not None:
            value = await _query(conn, "AND t.transaction_id = $3")
            if value is not None:
                return value
            return await _query(
                conn,
                "AND (t.transaction_id IS NULL OR t.transaction_id = $3)",
            )
        return await _query(conn, "")


def _duration_s(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if start is None or end is None:
        return None
    delta = end - start
    seconds = delta.total_seconds()
    if seconds < 0:
        return None
    return int(seconds)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def write_session_log_reconciliation(
    ts_pool: asyncpg.Pool,
    *,
    session_id: UUID,
    log_import_id: UUID,
    result: ReconciliationResult,
    conn: Optional[asyncpg.Connection] = None,
) -> bool:
    """Idempotent UPSERT of the reconciliation row.

    Unique key is ``(session_id, log_import_id)`` so re-running the
    reconciler for the same import overwrites the prior row rather
    than appending. Returns ``True`` on success.
    """
    notes_json = json.dumps(result.notes or {}, default=str)
    sql = """
        INSERT INTO session_log_reconciliations (
            session_id, log_import_id, computed_at,
            our_energy_kwh, charger_energy_kwh, energy_delta_pct,
            our_duration_s, charger_duration_s,
            our_start_time, charger_start_time,
            our_end_time, charger_end_time,
            source, notes
        ) VALUES (
            $1, $2, NOW(),
            $3, $4, $5,
            $6, $7,
            $8, $9,
            $10, $11,
            $12, $13::jsonb
        )
        ON CONFLICT (session_id, log_import_id) DO UPDATE SET
            computed_at        = EXCLUDED.computed_at,
            our_energy_kwh     = EXCLUDED.our_energy_kwh,
            charger_energy_kwh = EXCLUDED.charger_energy_kwh,
            energy_delta_pct   = EXCLUDED.energy_delta_pct,
            our_duration_s     = EXCLUDED.our_duration_s,
            charger_duration_s = EXCLUDED.charger_duration_s,
            our_start_time     = EXCLUDED.our_start_time,
            charger_start_time = EXCLUDED.charger_start_time,
            our_end_time       = EXCLUDED.our_end_time,
            charger_end_time   = EXCLUDED.charger_end_time,
            source             = EXCLUDED.source,
            notes              = EXCLUDED.notes
    """
    args = (
        session_id,
        log_import_id,
        result.our_energy_kwh,
        result.charger_energy_kwh,
        result.energy_delta_pct,
        result.our_duration_s,
        result.charger_duration_s,
        result.our_start_time,
        result.charger_start_time,
        result.our_end_time,
        result.charger_end_time,
        result.source,
        notes_json,
    )
    if conn is not None:
        await conn.execute(sql, *args)
    else:
        async with ts_pool.acquire() as pooled:
            await pooled.execute(sql, *args)
    return True


__all__ = [
    "ReconciliationResult",
    "ReconciliationSource",
    "reconcile_session_log",
    "write_session_log_reconciliation",
]
