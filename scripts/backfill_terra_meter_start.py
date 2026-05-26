#!/usr/bin/env python3
"""One-shot backfill for charging_sessions rows with NULL energy_delivered_kwh.

Use this script after deploying Phases 1 and 2 of the Terra AC fix
(see CLAUDE.md and ``meter_value_utils.synthesize_energy_kwh_from_meter_stop``)
to recover historical sessions that were stuck with
``energy_delivered_kwh = NULL`` because either:

  * ``meter_start_wh = 0`` (legacy poison from the old behaviour where
    StartTransaction's ``meterStart=0`` was persisted as-is — Phase 1 now
    coerces to NULL), OR
  * ``meter_start_wh IS NULL`` and ``energy_delivered_kwh IS NULL`` (the
    new "deferred but never backfilled" case for chargers whose
    measurand config did not enable Energy.Active.Import.Register).

For each candidate row the script attempts, in order:

  1. **Register-sample backfill** — find the earliest
     ``energy_kwh`` reading in ``telemetry`` for the session's
     transaction window (``telemetry_samples`` was retired in
     migration 045; energy_kwh is the same data, normalised to kWh).
     If one exists, use it as the effective ``meter_start_wh`` and
     compute the bracket-based delta against ``last_meter_wh`` (or
     ``meter_stop_wh`` if the running register column is also NULL).
  2. **Synthesised delta** — if no register samples exist for the
     session AND ``meter_stop_wh`` is positive AND below the
     ``OCPP_SYNTHESIZED_DELTA_CAP_KWH`` cap (default 50 kWh), treat
     ``meter_stop_wh`` itself as the per-session delta and suffix
     ``stop_reason`` with ``|synthesized_delta`` for audit.
  3. **Skip** — leave the row NULL when neither path applies.

Dry-run by default. Use ``--apply`` to commit. Always emits a
per-row decision line and a summary.

Usage::

  python scripts/backfill_terra_meter_start.py [--apply]
                                               [--limit N]
                                               [--station-id ID]
                                               [--cap-kwh N]

Exit codes:
  0 — completed successfully (dry-run or apply).
  1 — connection / fatal error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from db.postgres_url import prepare_asyncpg_url_and_ssl  # noqa: E402

from websocket_handler.meter_value_utils import (  # noqa: E402
    DEFAULT_SYNTHESIZED_DELTA_CAP_WH,
    compute_energy_kwh,
    synthesize_energy_kwh_from_meter_stop,
)


def _resolve_database_url() -> str:
    """Pick the same URL the prod handler uses, with the same precedence."""
    url = os.getenv("TIMESCALE_SERVICE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Set TIMESCALE_SERVICE_URL or DATABASE_URL to point at the "
            "TimescaleDB instance to backfill."
        )
    return url


def _resolve_cap_wh(cli_kwh: Optional[float]) -> int:
    """CLI overrides the env var; env overrides the default."""
    if cli_kwh is not None:
        if cli_kwh > 0:
            return int(cli_kwh * 1000)
        return DEFAULT_SYNTHESIZED_DELTA_CAP_WH
    env_raw = os.getenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH")
    if env_raw:
        try:
            kwh = float(env_raw)
        except ValueError:
            return DEFAULT_SYNTHESIZED_DELTA_CAP_WH
        if kwh > 0:
            return int(kwh * 1000)
    return DEFAULT_SYNTHESIZED_DELTA_CAP_WH


_CANDIDATE_SQL = """
    SELECT
        session_id,
        station_id,
        transaction_id,
        start_time,
        end_time,
        meter_start_wh,
        meter_stop_wh,
        last_meter_wh,
        stop_reason
    FROM charging_sessions
    WHERE source = 'live'
      AND end_time IS NOT NULL
      AND energy_delivered_kwh IS NULL
      AND (meter_start_wh IS NULL OR meter_start_wh = 0)
      {station_filter}
    ORDER BY end_time DESC
    LIMIT $1
"""


_EARLIEST_REGISTER_SQL = """
    SELECT ROUND(MIN(energy_kwh) * 1000)::bigint AS earliest_wh
    FROM telemetry
    WHERE station_id     = $1
      AND transaction_id = $2
      AND energy_kwh IS NOT NULL
      AND energy_kwh > 0
      AND time BETWEEN $3 AND $4
"""


_APPLY_SQL = """
    UPDATE charging_sessions
       SET meter_start_wh       = COALESCE($2, meter_start_wh),
           energy_delivered_kwh = $3,
           stop_reason          = COALESCE($4, stop_reason),
           updated_at           = NOW()
     WHERE session_id = $1
       AND source     = 'live'
       AND end_time IS NOT NULL
       AND energy_delivered_kwh IS NULL
"""


async def _earliest_register_wh(
    conn: asyncpg.Connection,
    station_id: str,
    transaction_id: int,
    start_time,
    end_time,
) -> Optional[int]:
    """Find the earliest positive Energy.Active.Import.Register Wh for the session."""
    val = await conn.fetchval(
        _EARLIEST_REGISTER_SQL,
        station_id,
        transaction_id,
        start_time,
        end_time,
    )
    return int(val) if val is not None else None


async def _classify_and_backfill(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    cap_wh: int,
    apply: bool,
) -> str:
    """Decide what to write for a single stuck row, applying when requested.

    Returns a one-token decision used for the run summary:
      ``register`` — backfilled from a register sample
      ``synthesized`` — Phase 2 synthesis from meter_stop_wh
      ``skipped`` — neither path applies, row stays NULL
    """
    session_id = row["session_id"]
    station_id = row["station_id"]
    transaction_id = row["transaction_id"]
    meter_start = row["meter_start_wh"]
    meter_stop = row["meter_stop_wh"]
    last_meter = row["last_meter_wh"]
    stop_reason = row["stop_reason"]

    earliest = None
    if transaction_id is not None:
        earliest = await _earliest_register_wh(
            conn,
            station_id,
            int(transaction_id),
            row["start_time"],
            row["end_time"],
        )

    new_meter_start: Optional[int] = None
    new_energy_kwh: Optional[float] = None
    new_stop_reason: Optional[str] = None
    decision = "skipped"

    if earliest is not None:
        # Path 1: register-sample backfill. Use the running register
        # (last_meter_wh) when present — it's monotonic and always ≥
        # earliest. Fall back to meter_stop_wh otherwise.
        terminal = last_meter if last_meter is not None else meter_stop
        new_energy_kwh = compute_energy_kwh(terminal, earliest)
        if new_energy_kwh is not None:
            new_meter_start = earliest
            decision = "register"
    elif meter_stop is not None and meter_stop > 0 and last_meter is None:
        # Path 2: Phase 2 synthesis. Triple-NULL state (no start, no
        # running register), meter_stop is the only signal.
        synthesized = synthesize_energy_kwh_from_meter_stop(meter_stop, cap_wh=cap_wh)
        if synthesized is not None:
            new_energy_kwh = synthesized
            base = stop_reason or "unknown"
            new_stop_reason = f"{base}|synthesized_delta"[:64]
            decision = "synthesized"

    print(
        f"[{decision:>11}] session={session_id} station={station_id} "
        f"tx_id={transaction_id} "
        f"meter_start_wh: {meter_start!r} -> {new_meter_start!r}, "
        f"last_meter_wh={last_meter!r}, meter_stop_wh={meter_stop!r}, "
        f"energy_delivered_kwh -> "
        f"{None if new_energy_kwh is None else round(new_energy_kwh, 3)}",
        flush=True,
    )

    if decision != "skipped" and apply:
        await conn.execute(
            _APPLY_SQL,
            session_id,
            new_meter_start,
            new_energy_kwh,
            new_stop_reason,
        )

    return decision


async def _run(
    apply: bool,
    limit: int,
    station_id_filter: Optional[str],
    cap_wh: int,
) -> int:
    asyncpg_url, ssl_ctx = prepare_asyncpg_url_and_ssl(_resolve_database_url())

    conn = await asyncpg.connect(asyncpg_url, ssl=ssl_ctx)
    counts: Counter = Counter()
    try:
        if station_id_filter:
            sql = _CANDIDATE_SQL.format(station_filter="AND station_id = $2")
            rows = await conn.fetch(sql, limit, station_id_filter)
        else:
            sql = _CANDIDATE_SQL.format(station_filter="")
            rows = await conn.fetch(sql, limit)

        if not rows:
            print("No candidate rows found. Nothing to do.")
            return 0

        try:
            async with conn.transaction():
                for row in rows:
                    decision = await _classify_and_backfill(conn, row, cap_wh, apply)
                    counts[decision] += 1
                if not apply:
                    # Dry-run inside a transaction — rollback at the end so any
                    # accidental writes (there shouldn't be any) are undone.
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass
    finally:
        await conn.close()

    total = sum(counts.values())
    print()
    print(f"Summary: {total} candidate rows examined")
    print(f"  register-backfilled: {counts['register']}")
    print(f"  synthesized:         {counts['synthesized']}")
    print(f"  skipped:             {counts['skipped']}")
    print(f"  mode:                {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  cap_kwh:             {cap_wh / 1000}")
    return 0


class _DryRunRollback(Exception):
    """Sentinel raised inside the transaction to force a dry-run rollback."""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit changes (default is dry-run).")
    p.add_argument("--limit", type=int, default=1000, help="Maximum rows to process per run.")
    p.add_argument(
        "--station-id",
        type=str,
        default=None,
        help="Restrict to a single station_id (handy for verifying a specific charger).",
    )
    p.add_argument(
        "--cap-kwh",
        type=float,
        default=None,
        help="Override synthesized-delta cap (kWh). Falls back to "
        "OCPP_SYNTHESIZED_DELTA_CAP_KWH env var, then 50.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cap_wh = _resolve_cap_wh(args.cap_kwh)
    return asyncio.run(_run(args.apply, args.limit, args.station_id, cap_wh))


if __name__ == "__main__":
    raise SystemExit(main())
