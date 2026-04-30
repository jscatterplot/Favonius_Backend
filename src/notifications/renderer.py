"""Render notification_alerts rows into EmailMessage payloads.

Templates live in src/notifications/templates/email/. Lookup convention:
  - <alert_type>.html.j2   → HTML body
  - <alert_type>.txt.j2    → plain-text body
  - subject.txt.j2         → subject line (one line)

If a template for a specific alert_type is missing, the renderer falls back
to `generic.<format>.j2` so any new alert type still produces a sane email
without requiring a new template up front.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from .alerts import Alert
from .email_client import EmailMessage

logger = logging.getLogger(__name__)


_TEMPLATE_DIR = Path(__file__).parent / "templates" / "email"


def _autoescape_for(template_name: str | None) -> bool:
    """Autoescape only HTML templates; text emails keep literal characters."""
    return bool(template_name) and ".html." in template_name


@lru_cache(maxsize=1)
def _env() -> Environment:
    """Module-level singleton Jinja2 Environment."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=_autoescape_for,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@dataclass(frozen=True)
class RenderedAlert:
    subject: str
    html: str
    text: str


def _select_template(alert_type: str, fmt: str) -> str:
    """Return the template name to load for (alert_type, fmt).

    Tries `<alert_type>.<fmt>.j2` first; falls back to `generic.<fmt>.j2`.
    fmt ∈ {'html', 'txt'}.
    """
    specific = f"{alert_type}.{fmt}.j2"
    generic = f"generic.{fmt}.j2"
    env = _env()
    try:
        env.get_template(specific)
        return specific
    except TemplateNotFound:
        return generic


def render_alert(alert: Alert, *, recipient_email: str, from_address: str) -> EmailMessage:
    """Render a fully-populated EmailMessage for a recipient.

    All field substitution happens in templates; the renderer just hands the
    Alert + a small `ctx` dict to Jinja. Templates can call `e()` autoescape
    for arbitrary HTML safety on user-supplied detail values.
    """
    env = _env()
    ctx = _build_context(alert)

    subject = env.get_template("subject.txt.j2").render(**ctx).strip()
    html = env.get_template(_select_template(alert.alert_type, "html")).render(**ctx)
    text = env.get_template(_select_template(alert.alert_type, "txt")).render(**ctx)

    return EmailMessage(
        to=recipient_email,
        subject=subject,
        html=html,
        text=text,
        from_address=from_address,
        headers={
            "X-Favonius-Alert-Id": str(alert.id),
            "X-Favonius-Alert-Type": alert.alert_type,
            "X-Favonius-Severity": alert.severity.value,
        },
    )


def _build_context(alert: Alert) -> dict[str, Any]:
    return {
        "alert": alert,
        "alert_id": alert.id,
        "alert_type": alert.alert_type,
        "severity": alert.severity.value,
        "severity_display": alert.severity.value.upper(),
        "title": alert.title,
        "detail": alert.detail,
        "first_occurrence_at": alert.first_occurrence_at,
        "last_occurrence_at": alert.last_occurrence_at,
        "depot_id": alert.depot_id,
        "organization_id": alert.organization_id,
        "occurrence_count_text": _occurrence_text(alert),
    }


def _occurrence_text(alert: Alert) -> str:
    """Human-readable summary of when the alert first appeared and recurred."""
    if alert.first_occurrence_at == alert.last_occurrence_at:
        return f"first seen at {alert.first_occurrence_at:%Y-%m-%d %H:%M:%S UTC}"
    return (
        f"first seen at {alert.first_occurrence_at:%Y-%m-%d %H:%M:%S UTC}, "
        f"latest at {alert.last_occurrence_at:%Y-%m-%d %H:%M:%S UTC}"
    )


__all__ = ["RenderedAlert", "render_alert"]
