"""Per-org monthly token-budget for the SQL-mode chat agent (PLAN.md §S4).

This module owns the depot chat agent's cost-protection budget. Two layers:

* **Config resolution** — :func:`resolve_org_token_budget` returns the effective
  monthly token ceiling for an organization.
* **Enforcement** — :class:`TokenBudgetTracker` (``check_and_reserve`` /
  ``record_actual``) maintains an in-process counter against that ceiling and
  refuses a turn *before* it calls Anthropic when the org is over budget.

Budget precedence (highest wins):

1. **Per-org column** — ``organizations.agent_token_budget_monthly`` (Supabase
   static, migration ``supabase/044``) when set AND a positive integer. This is
   a per-company commercial attribute (different customers buy different
   amounts), so it lives as a first-class, editable column — not an env var.
2. **Platform default** — :data:`DEFAULT_TOKEN_BUDGET_MONTHLY` (10,000,000), the
   hard-coded fallback for orgs with no negotiated amount.

The resolver is **fail-open**: any DB error, a missing column (during the
``supabase/044`` rollout window), or an absent / blank / zero / non-positive
value all fall back to the platform default. Token accounting is
cost-protection, not billing, so reading the budget must never break a turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# Hard-coded platform default. 10M tokens/org/month is generous headroom for
# in-app operator chat while still capping a runaway loop's blast radius. A
# per-org ceiling is set on organizations.agent_token_budget_monthly (mig
# supabase/044) and takes precedence; this is the fallback when it's NULL.
DEFAULT_TOKEN_BUDGET_MONTHLY = 10_000_000


def _parse_positive_int(raw: Any) -> Optional[int]:
    """Return ``raw`` as a positive ``int``, or ``None`` if it isn't one.

    Accepts the text asyncpg hands back from a JSONB ``->>`` extraction
    (e.g. ``"5000000"``) as well as an already-typed int. Whitespace is
    stripped; zero, negatives, blanks, and non-numeric values yield ``None``
    so every "bad value" path collapses to a single fall-back-to-default
    decision in the caller.
    """
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def resolve_org_token_budget(
    static_pool: Any,
    organization_id: Optional[UUID],
) -> int:
    """Return the effective monthly token ceiling for ``organization_id``.

    Precedence: a positive ``organizations.agent_token_budget_monthly`` beats
    the platform default :data:`DEFAULT_TOKEN_BUDGET_MONTHLY`. See the module
    docstring.

    Fail-open by contract — returns the platform default when there is no org,
    no pool, the value is unset/garbage, or the read raises. The column is read
    via ``to_jsonb(o)->>'agent_token_budget_monthly'`` rather than selecting the
    column directly so a database where ``supabase/044`` has not yet applied
    returns NULL (→ default) instead of raising ``UndefinedColumn`` every turn
    (mirrors the resilient ``to_jsonb(row)->>'col'`` reads in
    ``src/core/state/assembler.py``).

    Args:
        static_pool: asyncpg pool for the Supabase static schema (where
            ``organizations`` lives). ``None`` short-circuits to the default.
        organization_id: The caller's org. ``None`` (e.g. ``favonius_admin``)
            short-circuits to the default — no per-org value applies.

    Returns:
        The monthly token budget as a positive ``int``.
    """
    default = DEFAULT_TOKEN_BUDGET_MONTHLY
    if static_pool is None or organization_id is None:
        return default

    try:
        async with static_pool.acquire() as conn:
            raw = await conn.fetchval(
                """
                SELECT to_jsonb(o) ->> 'agent_token_budget_monthly'
                FROM organizations o
                WHERE o.id = $1::uuid
                """,
                str(organization_id),
            )
    except Exception:  # noqa: BLE001 — fail open: budget read must never break a turn
        logger.warning(
            "Failed to read per-org token budget for org %s; using platform default %d",
            organization_id,
            default,
            exc_info=True,
        )
        return default

    override = _parse_positive_int(raw)
    return override if override is not None else default


# ── Enforcement (S4-B) ──────────────────────────────────────────────────────
#
# An in-process counter is authoritative for a turn's accept/refuse decision;
# the agent_token_usage table is a best-effort durable mirror (flushed in the
# background) and the cold-start hydration source. The budget must never block
# or break a turn — every DB touch fails open.

# Per-turn reservation estimate: ~system-prompt tokens + this flat overhead.
PER_TURN_OVERHEAD_TOKENS = 4096

# Background-flush cadence: write to agent_token_usage after this many
# record_actual calls OR this many seconds since the last flush, whichever
# comes first. Tuned for the /agent/* 10 req/min rate limit — a few dozen
# turns or half a minute bounds how much usage a crash can lose.
DEFAULT_FLUSH_EVERY_WRITES = 25
DEFAULT_FLUSH_EVERY_SECONDS = 30.0


def current_period_yyyymm(now: Optional[datetime] = None) -> str:
    """Return the current budget period as ``'YYYYMM'`` in UTC.

    UTC (not depot-local) so an org's monthly ceiling is one global window,
    not one per timezone. ``now`` is injectable for tests (period rollover).
    """
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y%m")


def estimate_turn_tokens(system_prompt: str) -> int:
    """Rough per-turn token reservation: system-prompt size + fixed overhead.

    Deliberately crude — cost-protection, not billing (``record_actual``
    reconciles to the measured total):

    * system-prompt tokens ≈ ``len(system_prompt) // 4`` (the standard
      ~4-chars-per-token rule of thumb), plus
    * a flat :data:`PER_TURN_OVERHEAD_TOKENS` covering the user message, tool
      schemas, the tool-result round-trips, and the model's output.

    A SQL turn can make several API calls, so the true spend is usually higher;
    this estimate is just the floor that gates the FIRST check, and
    ``record_actual`` replaces it with the real usage afterwards.
    """
    prompt_tokens = max(0, len(system_prompt or "") // 4)
    return prompt_tokens + PER_TURN_OVERHEAD_TOKENS


@dataclass(frozen=True)
class Reservation:
    """A successful pre-LLM hold returned by :meth:`TokenBudgetTracker.check_and_reserve`.

    Carries everything :meth:`record_actual` needs to reconcile the estimate
    against the measured usage — without re-resolving the period. An
    ``organization_id`` of ``None`` marks the fail-open no-op reservation
    handed back when the turn isn't org-scoped (e.g. favonius_admin); its
    ``record_actual`` is a no-op.
    """

    organization_id: Optional[str]
    period_yyyymm: str
    est_tokens: int


@dataclass
class _PeriodCounter:
    """In-process token tally for one (organization, period)."""

    committed_input: int = 0  # reconciled actuals + hydrated DB base
    committed_output: int = 0
    reserved: int = 0  # outstanding estimates for in-flight turns
    unflushed_input: int = 0  # actuals not yet written to agent_token_usage
    unflushed_output: int = 0

    @property
    def total(self) -> int:
        """Tokens counted against the ceiling: durable actuals + in-flight holds."""
        return self.committed_input + self.committed_output + self.reserved


class TokenBudgetTracker:
    """In-process per-(org, period) token counter with best-effort DB flush.

    Production uses the module singleton ``_TRACKER`` (via the module-level
    :func:`check_and_reserve` / :func:`record_actual` shims), which lazily
    borrows the lifespan asyncpg pools from ``src.api.main`` the same way
    ``src/api/agent/router.py`` does. Tests instantiate this class directly with
    fake pools and an injected ``period_provider`` / ``flush_every_writes`` so
    neither the clock nor a real DB is required.
    """

    def __init__(
        self,
        *,
        static_pool: Any = None,
        ts_pool: Any = None,
        flush_every_writes: int = DEFAULT_FLUSH_EVERY_WRITES,
        flush_every_seconds: float = DEFAULT_FLUSH_EVERY_SECONDS,
        ceiling_resolver: Callable[..., Any] = resolve_org_token_budget,
        period_provider: Callable[[], str] = current_period_yyyymm,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._explicit_static_pool = static_pool
        self._explicit_ts_pool = ts_pool
        self._flush_every_writes = max(1, int(flush_every_writes))
        self._flush_every_seconds = float(flush_every_seconds)
        self._resolve_ceiling = ceiling_resolver
        self._period_provider = period_provider
        self._monotonic = monotonic
        self._counters: dict[tuple[str, str], _PeriodCounter] = {}
        self._hydrated: set[tuple[str, str]] = set()
        # Serialises cold-start hydration so concurrent first-turn requests for
        # the same (org, period) all WAIT for the DB read instead of racing past
        # it against a zero baseline (and so the hydration assignment can't clobber
        # usage another turn committed in the meantime). asyncio.Lock binds to the
        # running loop lazily (3.10+), so constructing it here — including for the
        # module singleton at import time — is safe.
        self._hydration_lock = asyncio.Lock()
        self._writes_since_flush = 0
        self._last_flush_at = monotonic()
        self._flush_task: Optional[asyncio.Task] = None

    # — pool resolution ——————————————————————————————————————————————
    def _pools(self) -> tuple[Any, Any]:
        """Return ``(static_pool, ts_pool)`` — explicit if injected, else lifespan."""
        if self._explicit_static_pool is not None or self._explicit_ts_pool is not None:
            return self._explicit_static_pool, self._explicit_ts_pool
        try:
            from src.api import main as api_main  # local import: avoid circular import

            pools = api_main.db_pools
        except Exception:  # noqa: BLE001 — fail open if app state isn't wired
            return None, None
        if pools is None:
            return None, None
        return pools.static, pools.ts

    # — public API ————————————————————————————————————————————————————
    async def check_and_reserve(
        self, organization_id: Optional[UUID], est_tokens: int
    ) -> Optional[Reservation]:
        """Reserve ``est_tokens`` for ``organization_id`` this period.

        Returns a :class:`Reservation` when the turn fits under the org's
        monthly ceiling (resolved via :func:`resolve_org_token_budget`), or
        ``None`` when it would push the org over — the caller then refuses
        *before* invoking the LLM. A turn fits while ``current_usage +
        est_tokens <= ceiling``; it is refused once that sum would *exceed* the
        ceiling.

        ``organization_id=None`` (not org-scoped, e.g. favonius_admin) returns
        a no-op reservation rather than refusing — the budget is per-org.
        """
        period = self._period_provider()
        if organization_id is None:
            return Reservation(organization_id=None, period_yyyymm=period, est_tokens=0)

        est = max(0, int(est_tokens or 0))
        key = (str(organization_id), period)

        # Cold-start hydration + ceiling resolution both await; do them BEFORE
        # the synchronous check-and-reserve below so no await splits the
        # decision (which would let two concurrent turns read the same counter).
        await self._maybe_hydrate(key)
        counter = self._counters.setdefault(key, _PeriodCounter())
        # Pin before the ceiling await so reconcile cannot evict this past-period
        # counter while reserved is still zero (month rollover during _resolve_ceiling).
        counter.reserved += est
        ceiling = await self._resolve_ceiling(*self._ceiling_args(organization_id))
        if counter.total > ceiling:
            counter.reserved -= est
            return None
        return Reservation(organization_id=key[0], period_yyyymm=period, est_tokens=est)

    def record_actual(
        self,
        reservation: Optional[Reservation],
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """Reconcile a reservation against measured usage and queue a flush.

        Drops the rough ``reservation.est_tokens`` hold and folds in the real
        ``actual_input_tokens`` / ``actual_output_tokens`` (both for the live
        ceiling math and the durable agent_token_usage mirror). A ``None`` or
        no-op (no-org) reservation is ignored. Synchronous and non-blocking:
        the DB write is scheduled in the background, never awaited here.
        """
        if reservation is None or not reservation.organization_id:
            return
        key = (reservation.organization_id, reservation.period_yyyymm)
        counter = self._counters.setdefault(key, _PeriodCounter())
        ai = max(0, int(actual_input_tokens or 0))
        ao = max(0, int(actual_output_tokens or 0))
        counter.reserved = max(0, counter.reserved - reservation.est_tokens)
        counter.committed_input += ai
        counter.committed_output += ao
        counter.unflushed_input += ai
        counter.unflushed_output += ao
        self._writes_since_flush += 1
        self._maybe_schedule_flush()

    async def flush(self) -> None:
        """Persist accumulated actuals to agent_token_usage (append-style upsert).

        Best-effort: on any DB error the un-written deltas are rolled back into
        the in-process counters so the next flush retries them. Safe to call
        directly (tests / shutdown) as well as from the background task.
        """
        _, ts_pool = self._pools()
        if ts_pool is None:
            return
        # Snapshot + zero the deltas synchronously (no await) so record_actual
        # calls during the write land in the NEXT batch, not this one.
        pending: list[tuple[str, str, int, int]] = []
        for (org_id, period), counter in self._counters.items():
            if counter.unflushed_input or counter.unflushed_output:
                pending.append((org_id, period, counter.unflushed_input, counter.unflushed_output))
                counter.unflushed_input = 0
                counter.unflushed_output = 0
        if not pending:
            return
        try:
            async with ts_pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO agent_token_usage
                        (organization_id, period_yyyymm, input_tokens, output_tokens, last_updated)
                    VALUES ($1::uuid, $2, $3, $4, NOW())
                    ON CONFLICT (organization_id, period_yyyymm) DO UPDATE SET
                        input_tokens  = agent_token_usage.input_tokens  + EXCLUDED.input_tokens,
                        output_tokens = agent_token_usage.output_tokens + EXCLUDED.output_tokens,
                        last_updated  = NOW()
                    """,
                    pending,
                )
        except Exception:  # noqa: BLE001 — best-effort: never lose usage, never raise
            logger.warning(
                "agent_token_usage flush failed; rolling %d delta(s) back for retry",
                len(pending),
                exc_info=True,
            )
            for org_id, period, di, do in pending:
                counter = self._counters.get((org_id, period))
                if counter is not None:
                    counter.unflushed_input += di
                    counter.unflushed_output += do

    async def reconcile(self) -> None:
        """Flush pending usage, then refresh committed baselines from the DB.

        Driven on a timer by the API lifespan (``_agent_budget_reconcile_loop``)
        so two gaps the on-write flush can't cover are closed:

        * **Low-traffic flush** — a single turn that never triggers the
          write/time flush threshold still gets persisted within the loop
          interval, so a restart can't silently drop it (the spec's "T seconds"
          flush).
        * **Multi-worker drift** — under ``WEB_CONCURRENCY > 1`` each worker
          re-reads ``agent_token_usage`` and so sees other workers' flushed
          spend. ``committed := DB_total + local_unflushed`` keeps our
          not-yet-flushed actuals counted while folding in cross-worker totals,
          bounding aggregate over-spend to roughly one interval's traffic
          instead of letting it grow unbounded. (A single web worker remains the
          assumed deployment — see ``data_sources.scheduler.check_single_worker``
          — this just degrades gracefully when that doesn't hold.)

        Best-effort: a per-key read failure leaves that key on its prior
        baseline rather than aborting the sweep.
        """
        await self.flush()
        self._evict_settled_past_periods()
        _, ts_pool = self._pools()
        if ts_pool is None:
            return
        for key in list(self._counters.keys()):
            # Only refresh fully-hydrated keys. A key mid cold-start hydration is
            # in `_counters` but not yet in `_hydrated` (and holds the hydration
            # lock); re-hydrating it here would race that in-flight read and could
            # clobber its committed totals (Bugbot). Hydration is one-shot, so a
            # key already in `_hydrated` has no concurrent hydration to race.
            if key not in self._hydrated:
                continue
            try:
                async with ts_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT input_tokens, output_tokens
                        FROM agent_token_usage
                        WHERE organization_id = $1::uuid AND period_yyyymm = $2
                        """,
                        key[0],
                        key[1],
                    )
            except Exception:  # noqa: BLE001 — best-effort: skip this key, keep sweeping
                logger.warning("agent_token_usage re-hydration failed for %s", key, exc_info=True)
                continue
            counter = self._counters.get(key)
            if counter is None:
                continue
            db_in = int(row["input_tokens"] or 0) if row is not None else 0
            db_out = int(row["output_tokens"] or 0) if row is not None else 0
            # DB total (all workers' flushed usage) + our still-unflushed local
            # actuals. No double-count: unflushed is, by definition, not yet in
            # the DB row we just read.
            counter.committed_input = db_in + counter.unflushed_input
            counter.committed_output = db_out + counter.unflushed_output

    def _evict_settled_past_periods(self) -> None:
        """Drop fully-settled counters for periods other than the current one.

        Without this, ``_counters`` / ``_hydrated`` grow unbounded in a
        long-lived worker (one entry per (org, month) ever seen), and each
        reconcile tick would re-``SELECT`` every stale period forever (Codex).
        A past-period counter is safe to evict once it carries no un-flushed
        delta and no in-flight reservation — its usage is durably in
        ``agent_token_usage`` and a late turn for that period simply re-hydrates.
        Called after ``flush()`` so past-period deltas are already persisted.
        """
        current = self._period_provider()
        for key in list(self._counters.keys()):
            if key[1] == current:
                continue
            # Mid cold-start hydration: counter exists but DB read not finished.
            if key not in self._hydrated:
                continue
            counter = self._counters[key]
            if (
                counter.reserved == 0
                and counter.unflushed_input == 0
                and counter.unflushed_output == 0
            ):
                self._counters.pop(key, None)
                self._hydrated.discard(key)

    # — internals —————————————————————————————————————————————————————
    def _ceiling_args(self, organization_id: UUID) -> tuple[Any, Optional[UUID]]:
        """Args for the ceiling resolver: ``(static_pool, organization_id)``."""
        static_pool, _ = self._pools()
        return static_pool, organization_id

    async def _maybe_hydrate(self, key: tuple[str, str]) -> None:
        """Seed an (org, period) counter from agent_token_usage exactly once.

        Serialised by ``_hydration_lock``: the first caller does the DB read
        while any concurrent first-turn callers block on the lock, then return
        early once it's hydrated. This guarantees no turn reserves or records
        against a zero baseline before hydration completes, and that the
        ``committed_*`` assignment below can't clobber usage another turn
        committed (no commits happen until hydration finishes). The key is
        marked hydrated AFTER the read — but still marked on DB failure so a
        down DB isn't re-hit every turn (fail-open: base 0 under-counts → allows).
        """
        if key in self._hydrated:
            return
        async with self._hydration_lock:
            # Re-check under the lock — another coroutine may have hydrated while
            # we waited.
            if key in self._hydrated:
                return
            self._counters.setdefault(key, _PeriodCounter())
            _, ts_pool = self._pools()
            if ts_pool is None:
                self._hydrated.add(key)
                return
            try:
                async with ts_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT input_tokens, output_tokens
                        FROM agent_token_usage
                        WHERE organization_id = $1::uuid AND period_yyyymm = $2
                        """,
                        key[0],
                        key[1],
                    )
            except Exception:  # noqa: BLE001 — fail open: base 0 under-counts, never blocks
                logger.warning(
                    "agent_token_usage hydration failed for %s; starting from 0",
                    key,
                    exc_info=True,
                )
                self._hydrated.add(key)
                return
            if row is not None:
                counter = self._counters[key]
                counter.committed_input = int(row["input_tokens"] or 0)
                counter.committed_output = int(row["output_tokens"] or 0)
            self._hydrated.add(key)

    def _maybe_schedule_flush(self) -> None:
        """Schedule a background flush when the write/time threshold is hit."""
        due = (
            self._writes_since_flush >= self._flush_every_writes
            or (self._monotonic() - self._last_flush_at) >= self._flush_every_seconds
        )
        if not due:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return  # a flush is already in flight
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (sync test context): skip background flush
        # Reset cadence counters now so concurrent record_actual calls don't
        # each schedule a duplicate flush; the write happens in the task.
        self._writes_since_flush = 0
        self._last_flush_at = self._monotonic()
        self._flush_task = loop.create_task(self.flush())


# ── Module singleton + public functions (the controller's entry points) ──────

_TRACKER = TokenBudgetTracker()


async def check_and_reserve(
    organization_id: Optional[UUID], est_tokens: int
) -> Optional[Reservation]:
    """Module-level shim over the singleton — see :meth:`TokenBudgetTracker.check_and_reserve`."""
    return await _TRACKER.check_and_reserve(organization_id, est_tokens)


def record_actual(
    reservation: Optional[Reservation],
    actual_input_tokens: int,
    actual_output_tokens: int,
) -> None:
    """Module-level shim over the singleton — see :meth:`TokenBudgetTracker.record_actual`."""
    _TRACKER.record_actual(reservation, actual_input_tokens, actual_output_tokens)


async def reconcile() -> None:
    """Module-level shim over the singleton — see :meth:`TokenBudgetTracker.reconcile`.

    Called on a timer by the API lifespan so low-traffic usage is persisted and
    cross-worker totals are folded in (see :meth:`TokenBudgetTracker.reconcile`).
    """
    await _TRACKER.reconcile()


async def flush_now() -> None:
    """Flush any pending in-process usage to ``agent_token_usage`` (e.g. shutdown)."""
    await _TRACKER.flush()
