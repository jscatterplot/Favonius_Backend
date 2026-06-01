"""Proactive scheduled-report suggestions mined from chat history.

After each *successful consumption* chat turn the controller calls
:func:`maybe_emit_automation_suggestion` (best-effort, flag-gated, time-boxed).
It mines the caller's recent ``agent_runs`` for a recurring consumption-query
shape, maps that shape to a ``monthly_consumption`` report schedule, and — when
the habit crosses a threshold — emits a pending ``schedule_suggestion``
agent_action that the today view surfaces as *"I can set this up. Should I?"*.
Approving the card (``agents.action.approve``) creates the schedule server-side.

Design notes:

* The detection core (:func:`detect_suggestions`) is **pure and deterministic**
  — no I/O, no metrics — so it is unit-tested in isolation. The async wrapper
  (:func:`maybe_emit_automation_suggestion`) does the DB reads, metrics, and the
  dedup layers.
* It never raises into the chat turn: the wrapper swallows everything and is
  bounded by ``AGENT_AUTOMATION_TIMEOUT_S`` so a slow/failed detection can never
  slow or break the user's reply.
* v1 scope is consumption / RFID-session reports only (``monthly_consumption``
  grouped by ``card`` or ``vehicle``) — the only report kind that renders real
  data and the dominant chat intent (``consumption_by_user``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional
from uuid import UUID

from src.api.agent.feature_flag import is_automation_suggestions_enabled

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.api.agent.auth_context import AuthContext

logger = logging.getLogger(__name__)

# ── Domain constants ──────────────────────────────────────────────────────────

Cadence = Literal["weekly", "monthly"]
GroupDim = Literal["card", "vehicle"]

_CONSUMPTION_INTENT = "consumption_by_user"
_ACTION_CLASS = "schedule_suggestion"
_AGENT_TYPE = "automation"
_REPORT_KIND = "monthly_consumption"

# Relative time-window phrase → cadence. ``today``/``yesterday`` are deliberately
# absent: report_schedules has no daily frequency, so daily habits are skipped.
_RELATIVE_TO_CADENCE: dict[str, Cadence] = {
    "last_month": "monthly",
    "this_month": "monthly",
    "last_week": "weekly",
    "this_week": "weekly",
}

# Spec weekday names (0=Sun..6=Sat), matching report_schedule_timing's convention.
_WEEKDAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

# Recent successful consumption turns for one user. status/intent filtered in SQL
# so the JSONB the detector parses is bounded; LIMIT caps the scan.
_RECENT_RUNS_SQL = """
SELECT run_id, user_message, final_intent, steps_json, status, created_at
FROM agent_runs
WHERE user_id = $1::uuid
  AND status = 'success'
  AND final_intent = 'consumption_by_user'
  AND created_at >= NOW() - ($2::int * INTERVAL '1 day')
ORDER BY created_at DESC
LIMIT 200
"""


# ── Dataclasses ─────────────────────────────────────────────────────────────--


@dataclass(frozen=True)
class SuggestionSignature:
    """Cluster key for recurring queries; also the dedup discriminator."""

    intent: str
    group_by: GroupDim
    cadence: Cadence

    def key(self) -> str:
        """Stable string persisted as ``payload.signatureKey`` and indexed."""
        return f"{self.intent}|{self.group_by}|{self.cadence}"


@dataclass(frozen=True)
class Suggestion:
    """A single proposed automation, ready to surface + (on approve) create."""

    signature: SuggestionSignature
    schedule_input: dict  # a ScheduleCreatePayload (camelCase) for normalize_create_payload
    summary: str  # the human-readable "Should I set this up?" text
    occurrence_count: int
    lookback_days: int
    sample_messages: list[str]
    first_seen: Optional[str]
    last_seen: Optional[str]


@dataclass(frozen=True)
class SuggestionConfig:
    """Tunable thresholds + proposed-schedule defaults (env-overridable)."""

    min_occurrences: int = 3
    lookback_days: int = 30
    reject_cooldown_days: int = 60
    max_sample_messages: int = 3
    default_time_of_day: str = "08:00"
    default_day_of_week: int = 1  # Monday (spec 0=Sun..6=Sat)
    default_day_of_month: int = 1
    default_autonomy_mode: str = "proposed"
    default_format: str = "pdf"

    @classmethod
    def from_env(cls) -> "SuggestionConfig":
        """Build from ``AGENT_AUTOMATION_*`` env vars, falling back to defaults."""
        return cls(
            min_occurrences=_int_env("AGENT_AUTOMATION_MIN_OCCURRENCES", 3, minimum=1),
            lookback_days=_int_env("AGENT_AUTOMATION_LOOKBACK_DAYS", 30, minimum=1),
            reject_cooldown_days=_int_env("AGENT_AUTOMATION_REJECT_COOLDOWN_DAYS", 60, minimum=0),
            default_time_of_day=os.environ.get("AGENT_AUTOMATION_DEFAULT_TIME", "08:00"),
            default_autonomy_mode=os.environ.get("AGENT_AUTOMATION_DEFAULT_AUTONOMY", "proposed"),
        )


def _int_env(name: str, default: int, *, minimum: int) -> int:
    """Parse a non-negative int env var, clamping to ``minimum`` on bad input."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


def _detect_timeout_s() -> float:
    raw = os.environ.get("AGENT_AUTOMATION_TIMEOUT_S", "2.0")
    try:
        return max(float(raw), 0.1)
    except (TypeError, ValueError):
        return 2.0


# ── Pure detection core ─────────────────────────────────────────────────────--


def _coerce_steps(steps_json: Any) -> list:
    """Return steps_json as a list, tolerating asyncpg str-encoded JSONB."""
    if isinstance(steps_json, list):
        return steps_json
    if isinstance(steps_json, str):
        try:
            parsed = json.loads(steps_json)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _extract_plan_payload(steps_json: Any) -> Optional[dict]:
    """Pull the ``extract_plan`` step payload (the QueryPlan dict) from a run."""
    for step in _coerce_steps(steps_json):
        if isinstance(step, dict) and step.get("name") == "extract_plan":
            payload = step.get("payload")
            if isinstance(payload, dict):
                return payload
    return None


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _absolute_span_days(from_iso: Any, to_iso: Any) -> Optional[int]:
    if not isinstance(from_iso, str) or not isinstance(to_iso, str):
        return None
    try:
        start = _parse_iso(from_iso)
        end = _parse_iso(to_iso)
    except ValueError:
        return None
    return abs((end - start).days)


def _cadence_from_time_window(time_window: Any) -> Optional[Cadence]:
    """Map a QueryPlan time window to a schedule cadence (or None to skip)."""
    if not isinstance(time_window, dict):
        return None
    kind = time_window.get("kind")
    if kind == "relative":
        relative = time_window.get("relative")
        return _RELATIVE_TO_CADENCE.get(relative) if isinstance(relative, str) else None
    if kind == "absolute":
        span = _absolute_span_days(time_window.get("from_iso"), time_window.get("to_iso"))
        if span is None:
            return None
        if 26 <= span <= 32:
            return "monthly"
        if 6 <= span <= 8:
            return "weekly"
        return None
    return None


def _group_dim_from_plan(plan: dict) -> GroupDim:
    """Infer the report grouping dimension from the QueryPlan.

    A ``vehicle`` group-by or vehicle subject → a per-vehicle report. Everything
    else (driver/RFID subjects, depot-wide totals, driver/day/month grouping)
    maps to a per-card report: driver consumption is realised via the driver's
    RFID cards, and the per-card breakdown is the common accounting view —
    matching the "RFID session logs" framing of this feature.
    """
    group_by = plan.get("group_by")
    group_by_list = group_by if isinstance(group_by, list) else []
    subjects = plan.get("subjects")
    subject_kinds = (
        {s.get("kind") for s in subjects if isinstance(s, dict)}
        if isinstance(subjects, list)
        else set()
    )
    if "vehicle" in group_by_list or "vehicle" in subject_kinds:
        return "vehicle"
    return "card"


def _title_for(signature: SuggestionSignature) -> str:
    cadence = "Weekly" if signature.cadence == "weekly" else "Monthly"
    return f"{cadence} consumption by {signature.group_by}"


def _schedule_input_from_signature(
    signature: SuggestionSignature,
    config: SuggestionConfig,
    requester_email: Optional[str],
) -> dict:
    """Build the ScheduleCreatePayload the suggestion would create on approval.

    The dict is shaped to pass ``normalize_create_payload`` unchanged: it
    satisfies the cadence rule (weekly⇒dayOfWeek set + dayOfMonth null;
    monthly⇒dayOfMonth set + dayOfWeek null), the groupBy ∈ {card,vehicle} rule,
    and the ``monthly_consumption ⇒ groupBy required`` rule.
    """
    monthly = signature.cadence == "monthly"
    recipients = (
        [{"emailAddress": requester_email, "format": config.default_format}]
        if requester_email
        else []
    )
    return {
        "name": _title_for(signature),
        "kind": _REPORT_KIND,
        "groupBy": signature.group_by,
        "frequency": signature.cadence,
        "dayOfMonth": config.default_day_of_month if monthly else None,
        "dayOfWeek": None if monthly else config.default_day_of_week,
        "timeOfDay": config.default_time_of_day,
        "autonomyMode": config.default_autonomy_mode,
        "isActive": True,
        "recipients": recipients,
    }


def build_summary(
    signature: SuggestionSignature,
    schedule_input: dict,
    *,
    depot_label: Optional[str] = None,
) -> str:
    """The human-readable "Should I set this up?" proposal text."""
    grouping = "by card" if signature.group_by == "card" else "by vehicle"
    if signature.cadence == "weekly":
        day_name = _WEEKDAY_NAMES[schedule_input["dayOfWeek"]]
        when = f"every {day_name} at {schedule_input['timeOfDay']}"
    else:
        when = (
            f"on day {schedule_input['dayOfMonth']} of each month at {schedule_input['timeOfDay']}"
        )
    where_to = (
        "and email it to you" if schedule_input["recipients"] else "and email it to your team"
    )
    location = f" ({depot_label})" if depot_label else ""
    return (
        f"You've pulled this consumption view ({grouping}) several times recently. "
        f"I can generate it as a PDF report {when}{location} {where_to}. "
        f"Should I set this up?"
    )


def _sample_messages(runs_sorted: list[dict], limit: int) -> list[str]:
    """Most-recent-first, deduped, length-capped sample of the user's questions."""
    seen: set[str] = set()
    out: list[str] = []
    for run in reversed(runs_sorted):  # runs_sorted is oldest→newest
        message = (run.get("user_message") or "").strip()
        if not message:
            continue
        key = message.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(message[:120])
        if len(out) >= limit:
            break
    return out


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _sort_key(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.min.replace(tzinfo=timezone.utc)


def _build_suggestion(
    signature: SuggestionSignature,
    runs_sorted: list[dict],
    config: SuggestionConfig,
    requester_email: Optional[str],
) -> Suggestion:
    schedule_input = _schedule_input_from_signature(signature, config, requester_email)
    return Suggestion(
        signature=signature,
        schedule_input=schedule_input,
        summary=build_summary(signature, schedule_input),
        occurrence_count=len(runs_sorted),
        lookback_days=config.lookback_days,
        sample_messages=_sample_messages(runs_sorted, config.max_sample_messages),
        first_seen=_iso(runs_sorted[0].get("created_at")),
        last_seen=_iso(runs_sorted[-1].get("created_at")),
    )


def detect_suggestions(
    runs: list[dict],
    *,
    config: SuggestionConfig,
    now: datetime,
    requester_email: Optional[str],
) -> list[Suggestion]:
    """Cluster recent runs into recurring signatures and emit candidate suggestions.

    Pure and side-effect free. Only successful ``consumption_by_user`` turns with
    a parseable plan and a weekly/monthly cadence are clustered; a signature must
    recur at least ``config.min_occurrences`` times to produce a Suggestion.
    Returned sorted by ``(occurrence_count desc, signature.key())`` for
    determinism.
    """
    cutoff = now - timedelta(days=config.lookback_days)
    clusters: dict[SuggestionSignature, list[dict]] = {}

    for run in runs:
        if run.get("status") != "success":
            continue
        if run.get("final_intent") != _CONSUMPTION_INTENT:
            continue
        created = run.get("created_at")
        if isinstance(created, datetime) and created < cutoff:
            continue
        plan = _extract_plan_payload(run.get("steps_json"))
        if plan is None:
            continue
        cadence = _cadence_from_time_window(plan.get("time_window"))
        if cadence is None:
            continue
        signature = SuggestionSignature(
            intent=_CONSUMPTION_INTENT,
            group_by=_group_dim_from_plan(plan),
            cadence=cadence,
        )
        clusters.setdefault(signature, []).append(run)

    suggestions: list[Suggestion] = []
    for signature, group_runs in clusters.items():
        if len(group_runs) < config.min_occurrences:
            continue
        runs_sorted = sorted(group_runs, key=lambda r: _sort_key(r.get("created_at")))
        suggestions.append(_build_suggestion(signature, runs_sorted, config, requester_email))

    suggestions.sort(key=lambda s: (-s.occurrence_count, s.signature.key()))
    return suggestions


# ── Persistence + dedup (async) ───────────────────────────────────────────────


def resolve_emit_depot(auth: "AuthContext") -> Optional[UUID]:
    """Pick the depot a suggestion is attributed to.

    A chat turn is cross-depot (``agent_runs.depot_id`` is NULL), but an
    ``agent_actions`` row needs a single depot. Use the caller's depot iff they
    have exactly one visible — otherwise skip (a favonius_admin sees them all and
    is not the target of accounting automations).
    """
    if len(auth.visible_depot_ids) == 1:
        return auth.visible_depot_ids[0]
    return None


def _action_payload(suggestion: Suggestion) -> dict:
    return {
        "signature": {**asdict(suggestion.signature), "key": suggestion.signature.key()},
        # Top-level mirror so the partial unique index (migration 047) can key on
        # a simple ->> path, matching the report_draft index pattern.
        "signatureKey": suggestion.signature.key(),
        "scheduleInput": suggestion.schedule_input,
        "evidence": {
            "occurrenceCount": suggestion.occurrence_count,
            "lookbackDays": suggestion.lookback_days,
            "sampleMessages": suggestion.sample_messages,
            "firstSeen": suggestion.first_seen,
            "lastSeen": suggestion.last_seen,
        },
    }


async def emit_schedule_suggestion_action(
    conn: Any,
    *,
    depot_id: str,
    mode: str,
    suggestion: Suggestion,
) -> bool:
    """Insert the pending ``schedule_suggestion`` agent_action.

    Mirrors ``report_schedules.emit_report_draft_action``: ON CONFLICT DO NOTHING
    against the migration-047 partial unique index makes concurrent turns
    single-flight. Returns True when a new row was inserted, False on conflict.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO agent_actions
            (depot_id, agent_type, action_class, mode, status, summary, payload)
        VALUES ($1::uuid, $2, $3, $4, 'pending', $5, $6::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id::text
        """,
        depot_id,
        _AGENT_TYPE,
        _ACTION_CLASS,
        mode,
        suggestion.summary,
        json.dumps(_action_payload(suggestion)),
    )
    return row is not None


async def _active_schedule_exists(conn: Any, depot_id: str, group_by: str, frequency: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM report_schedules
        WHERE depot_id = $1::uuid AND is_active
          AND kind = 'monthly_consumption' AND group_by = $2 AND frequency = $3
        LIMIT 1
        """,
        depot_id,
        group_by,
        frequency,
    )
    return row is not None


async def _recent_rejection_exists(
    conn: Any, depot_id: str, signature_key: str, cooldown_days: int
) -> bool:
    if cooldown_days <= 0:
        return False
    row = await conn.fetchrow(
        """
        SELECT 1 FROM agent_actions
        WHERE depot_id = $1::uuid
          AND action_class = 'schedule_suggestion'
          AND status = 'rejected'
          AND payload->>'signatureKey' = $2
          AND resolved_at >= NOW() - ($3::int * INTERVAL '1 day')
        LIMIT 1
        """,
        depot_id,
        signature_key,
        cooldown_days,
    )
    return row is not None


async def _detect_and_emit(
    ts_pool: Any,
    auth: "AuthContext",
    token_payload: dict,
    config: SuggestionConfig,
    now: datetime,
) -> None:
    """Resolve depot, mine history, apply dedup layers, emit at most a few cards."""
    # Lazy imports keep the pure-detector core (detect_suggestions + dataclasses)
    # free of the metrics / auth / report-rendering stacks, so its unit tests need
    # only the standard library.
    from src.api.report_schedules import resolve_autonomy_mode
    from src.monitoring.metrics import (
        AGENT_AUTOMATION_SUGGESTIONS,
        AGENT_AUTOMATION_SUPPRESSED,
    )
    from src.security.auth import get_user_email

    depot = resolve_emit_depot(auth)
    if depot is None:
        AGENT_AUTOMATION_SUPPRESSED.labels(reason="depot_unresolvable").inc()
        return
    depot_id = str(depot)

    rows = await ts_pool.fetch(_RECENT_RUNS_SQL, str(auth.user_id), config.lookback_days)
    runs = [dict(r) for r in rows]
    requester_email = get_user_email(token_payload)
    suggestions = detect_suggestions(runs, config=config, now=now, requester_email=requester_email)
    if not suggestions:
        return

    async with ts_pool.acquire() as conn:
        for suggestion in suggestions:
            AGENT_AUTOMATION_SUGGESTIONS.labels(stage="detected").inc()
            sig = suggestion.signature
            if await _active_schedule_exists(conn, depot_id, sig.group_by, sig.cadence):
                AGENT_AUTOMATION_SUPPRESSED.labels(reason="existing_schedule").inc()
                continue
            if await _recent_rejection_exists(
                conn, depot_id, sig.key(), config.reject_cooldown_days
            ):
                AGENT_AUTOMATION_SUPPRESSED.labels(reason="rejected_cooldown").inc()
                continue
            # Respect a depot that opted out by setting the matrix to 'shadow'.
            # v1 suggestions are always human-gated proposals (we never
            # auto-create a schedule), so any non-shadow level emits a proposal.
            mode_pref = await resolve_autonomy_mode(conn, depot_id, _ACTION_CLASS, "proposed")
            if mode_pref == "shadow":
                AGENT_AUTOMATION_SUPPRESSED.labels(reason="autonomy_shadow").inc()
                continue
            inserted = await emit_schedule_suggestion_action(
                conn, depot_id=depot_id, mode="proposed", suggestion=suggestion
            )
            if inserted:
                AGENT_AUTOMATION_SUGGESTIONS.labels(stage="emitted").inc()
                if not suggestion.schedule_input["recipients"]:
                    # Informational: emitted but with no default recipient (the
                    # caller's JWT carried no email); approver adds one later.
                    AGENT_AUTOMATION_SUPPRESSED.labels(reason="no_email").inc()
            else:
                AGENT_AUTOMATION_SUPPRESSED.labels(reason="duplicate_pending").inc()


async def maybe_emit_automation_suggestion(
    *,
    ts_pool: Any,
    auth: "AuthContext",
    token_payload: dict,
    config: Optional[SuggestionConfig] = None,
    now: Optional[datetime] = None,
) -> None:
    """Best-effort post-turn entry point. NEVER raises; NEVER blocks the reply.

    Flag-gated (``AGENT_AUTOMATION_SUGGESTIONS_ENABLED``, default off) and bounded
    by ``AGENT_AUTOMATION_TIMEOUT_S`` so a slow or failing detection cannot affect
    the user-facing turn. Runs after the reply is built and the run is closed.
    """
    if not is_automation_suggestions_enabled():
        return
    # Everything below is inside the try so this coroutine can NEVER raise into
    # the chat turn — including the lazy import, from_env(), and now(). An escape
    # here would land in run_turn's outer except and re-close an already-success
    # run as error, 502-ing a turn that actually succeeded.
    try:
        from src.monitoring.metrics import AGENT_AUTOMATION_DETECT_DURATION

        cfg = config or SuggestionConfig.from_env()
        current = now or datetime.now(timezone.utc)
        with AGENT_AUTOMATION_DETECT_DURATION.time():
            await asyncio.wait_for(
                _detect_and_emit(ts_pool, auth, token_payload, cfg, current),
                timeout=_detect_timeout_s(),
            )
    except Exception:  # noqa: BLE001 - best-effort; the turn already returned its reply
        logger.warning("automation suggestion detection failed (best-effort)", exc_info=True)
