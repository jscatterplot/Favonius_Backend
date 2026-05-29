"""Deterministic early-payment-window evaluation for traffic fines.

Pure, side-effect-free, and ``now``-injectable so it is exhaustively
unit-testable and reused unchanged by the deadline re-check sweep. No LLM, no
I/O. The LLM extracts the fields (``TrafficFineExtraction``); this module
decides whether the early-payment discount is closing within the alert window
and builds the operator-facing message verbatim per the product spec:

    Priority: Early payment discount for Fine #[ID] expires in 2 days.
    Automate payment now to save EUR[Discount Amount]?

(The "2 days" phrase is derived from the actual time remaining, and the
currency symbol from the fine's currency.)
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core.traffic_fines.models import EarlyPaymentEvaluation, TrafficFineExtraction

DEFAULT_ALERT_WINDOW_HOURS: float = 48.0

# Currency code -> display symbol. Anything not listed renders as the code
# followed by a space ("PLN 30"). EUR is the spec's example.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€",
    "GBP": "£",
    "USD": "$",
    "PLN": "zł ",
    "SEK": "kr ",
    "NOK": "kr ",
    "DKK": "kr ",
    "CHF": "CHF ",
}

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _currency_symbol(currency: Optional[str]) -> str:
    if not currency:
        return ""
    code = currency.strip().upper()
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def _resolve_timezone(depot_tz: Optional[str]) -> Union[ZoneInfo, timezone]:
    if not depot_tz:
        return timezone.utc
    try:
        return ZoneInfo(depot_tz)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse_deadline_utc(raw: str, tz: Union[ZoneInfo, timezone]) -> Optional[datetime]:
    """Parse an ISO date/datetime to a UTC instant.

    Date-only values resolve to end-of-day (23:59:59) in ``tz`` — the discount
    is good through the whole local day. Naive datetimes are assumed to be in
    ``tz``; aware datetimes keep their own offset. Returns None if unparseable.
    """
    text = raw.strip()
    if not text:
        return None
    if _DATE_ONLY_RE.match(text):
        try:
            d = date.fromisoformat(text)
        except ValueError:
            return None
        local = datetime.combine(d, time(23, 59, 59), tzinfo=tz)
        return local.astimezone(timezone.utc)
    # Normalise a trailing 'Z' for safety (3.11 handles it, but be explicit).
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _to_decimal(value: Optional[float]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d >= 0 else None


def _discount_amount(extraction: TrafficFineExtraction) -> Optional[Decimal]:
    full = _to_decimal(extraction.full_amount)
    early = _to_decimal(extraction.early_payment_amount)
    if full is not None and early is not None and full >= early:
        return full - early
    stated = _to_decimal(extraction.stated_discount_amount)
    if stated is not None:
        return stated
    return None


def _format_amount(amount: Decimal) -> str:
    # Whole numbers render without decimals (30); otherwise two places (12.50).
    if amount == amount.to_integral_value():
        return f"{int(amount)}"
    return f"{amount:.2f}"


def humanize_remaining(hours: float) -> str:
    """Human phrase for a positive remaining duration ('2 days', '18 hours')."""
    if hours < 24:
        n = max(1, ceil(hours))
        return f"{n} hour{'s' if n != 1 else ''}"
    n = ceil(hours / 24)
    return f"{n} day{'s' if n != 1 else ''}"


def _fine_id_display(extraction: TrafficFineExtraction, fallback_id: str) -> str:
    ref = (extraction.fine_reference or "").strip()
    return ref or fallback_id


def build_alert_message(
    *,
    fine_id_display: str,
    time_phrase: str,
    discount: Optional[Decimal],
    currency_symbol: str,
) -> str:
    """Build the operator-facing alert message verbatim per the spec."""
    if discount is not None and discount > 0:
        savings = f"{currency_symbol}{_format_amount(discount)}"
        tail = f"Automate payment now to save {savings}?"
    else:
        tail = "Automate payment now to keep the early-payment discount?"
    return (
        f"Priority: Early payment discount for Fine #{fine_id_display} "
        f"expires in {time_phrase}. {tail}"
    )


def evaluate_early_payment(
    extraction: TrafficFineExtraction,
    *,
    now: datetime,
    depot_tz: Optional[str] = None,
    fallback_fine_id: str = "",
    window_hours: float = DEFAULT_ALERT_WINDOW_HOURS,
) -> EarlyPaymentEvaluation:
    """Decide whether a fine's early-payment discount is closing soon.

    Args:
        extraction: The LLM-extracted fine fields.
        now: Current instant (timezone-aware UTC). Injected for testability;
            a naive value is assumed to be UTC.
        depot_tz: IANA timezone used to resolve date-only deadlines to
            end-of-day local time. Falls back to UTC when unset/invalid.
        fallback_fine_id: Shown as the '#ID' when the fine has no reference.
        window_hours: Alert when the deadline is within this many hours.

    Returns:
        An :class:`EarlyPaymentEvaluation`. ``within_window`` is True (and
        ``message`` populated) only for ``kind='within_window'``.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    fine_id_display = _fine_id_display(extraction, fallback_fine_id or "unknown")
    currency_symbol = _currency_symbol(extraction.currency)
    discount = _discount_amount(extraction)
    discount_float = float(discount) if discount is not None else None

    def _verdict(kind: str, **extra: object) -> EarlyPaymentEvaluation:
        return EarlyPaymentEvaluation(
            kind=kind,  # type: ignore[arg-type]
            fine_id_display=fine_id_display,
            discount_amount=discount_float,
            currency=extraction.currency,
            currency_symbol=currency_symbol,
            **extra,  # type: ignore[arg-type]
        )

    if not extraction.is_traffic_fine:
        return _verdict("not_a_fine")

    if not extraction.early_payment_deadline:
        return _verdict("no_deadline")

    tz = _resolve_timezone(depot_tz)
    deadline_utc = _parse_deadline_utc(extraction.early_payment_deadline, tz)
    if deadline_utc is None:
        return _verdict("no_deadline")

    hours_remaining = (deadline_utc - now).total_seconds() / 3600.0

    if hours_remaining <= 0:
        kind = "expired"
    elif hours_remaining <= window_hours:
        kind = "within_window"
    else:
        kind = "not_yet"

    message = None
    if kind == "within_window":
        message = build_alert_message(
            fine_id_display=fine_id_display,
            time_phrase=humanize_remaining(hours_remaining),
            discount=discount,
            currency_symbol=currency_symbol,
        )

    return _verdict(
        kind,
        within_window=(kind == "within_window"),
        hours_remaining=hours_remaining,
        deadline_utc=deadline_utc,
        message=message,
    )
