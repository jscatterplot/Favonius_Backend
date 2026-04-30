"""Unit tests for src.notifications.renderer.

Renders against the real templates shipped in
src/notifications/templates/email/. If a template is broken the test will
catch it before the dispatcher path does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from src.notifications.alerts import Alert
from src.notifications.renderer import render_alert
from src.notifications.severity import Severity


def _make_alert(
    *,
    alert_type: str = "charger_fault",
    severity: Severity = Severity.CRITICAL,
    title: str = "Charger CP001 connector 1: Faulted",
    detail: dict | None = None,
    first_at: datetime | None = None,
    last_at: datetime | None = None,
) -> Alert:
    return Alert(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        organization_id=uuid4(),
        depot_id=uuid4(),
        alert_type=alert_type,
        severity=severity,
        title=title,
        detail=detail
        or {
            "station_id": "CP001",
            "connector_id": 1,
            "status": "Faulted",
            "error_code": "PowerMeterFailure",
        },
        dedup_key="charger_fault:CP001:1",
        status="active",
        first_occurrence_at=first_at or datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc),
        last_occurrence_at=last_at or datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc),
        last_notified_at=None,
        last_notified_count=0,
    )


class TestSubjectLine:
    def test_subject_format(self):
        alert = _make_alert()
        msg = render_alert(alert, recipient_email="ops@x.com", from_address="alerts@favonius.energy")
        assert msg.subject == "[Favonius] CRITICAL: Charger CP001 connector 1: Faulted"

    def test_subject_uses_severity_uppercase(self):
        alert = _make_alert(severity=Severity.WARNING)
        msg = render_alert(alert, recipient_email="ops@x.com", from_address="from@x.com")
        assert "WARNING" in msg.subject
        assert "warning" not in msg.subject  # not lowercased

    def test_subject_is_single_line(self):
        msg = render_alert(_make_alert(), recipient_email="x@x.com", from_address="from@x.com")
        assert "\n" not in msg.subject
        assert "\r" not in msg.subject


class TestEnvelope:
    def test_recipient_and_from_are_set(self):
        msg = render_alert(
            _make_alert(),
            recipient_email="ops@example.com",
            from_address="alerts@favonius.energy",
        )
        assert msg.to == "ops@example.com"
        assert msg.from_address == "alerts@favonius.energy"

    def test_headers_include_tracking_metadata(self):
        msg = render_alert(_make_alert(), recipient_email="ops@x.com", from_address="from@x.com")
        assert msg.headers["X-Favonius-Alert-Id"] == "12345678-1234-5678-1234-567812345678"
        assert msg.headers["X-Favonius-Alert-Type"] == "charger_fault"
        assert msg.headers["X-Favonius-Severity"] == "critical"


class TestChargerFaultTemplate:
    def test_html_includes_charger_details(self):
        msg = render_alert(_make_alert(), recipient_email="x@x.com", from_address="from@x.com")
        assert "CP001" in msg.html
        assert "connector 1" in msg.html
        assert "Faulted" in msg.html
        assert "PowerMeterFailure" in msg.html

    def test_html_includes_severity_badge_class(self):
        msg = render_alert(
            _make_alert(severity=Severity.CRITICAL),
            recipient_email="x@x.com", from_address="from@x.com",
        )
        assert "severity-critical" in msg.html

    def test_text_body_has_no_html_tags(self):
        msg = render_alert(_make_alert(), recipient_email="x@x.com", from_address="from@x.com")
        assert "<p>" not in msg.text
        assert "<dl>" not in msg.text
        assert "<html" not in msg.text
        assert "CP001" in msg.text
        assert "PowerMeterFailure" in msg.text

    def test_omits_error_code_section_when_missing(self):
        detail = {"station_id": "CP002", "connector_id": 2, "status": "Faulted", "error_code": None}
        alert = _make_alert(detail=detail)
        msg = render_alert(alert, recipient_email="x@x.com", from_address="from@x.com")
        assert "Error code" not in msg.html
        assert "Error code" not in msg.text


class TestOccurrenceFormatting:
    def test_first_equals_last_says_first_seen(self):
        ts = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)
        msg = render_alert(
            _make_alert(first_at=ts, last_at=ts),
            recipient_email="x@x.com", from_address="from@x.com",
        )
        assert "first seen at 2026-04-30 10:00:00 UTC" in msg.html
        assert "latest at" not in msg.html

    def test_repeating_alert_shows_first_and_latest(self):
        first = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)
        last = datetime(2026, 4, 30, 10, 5, 30, tzinfo=timezone.utc)
        msg = render_alert(
            _make_alert(first_at=first, last_at=last),
            recipient_email="x@x.com", from_address="from@x.com",
        )
        assert "first seen at 2026-04-30 10:00:00 UTC" in msg.text
        assert "latest at 2026-04-30 10:05:30 UTC" in msg.text


class TestGenericFallback:
    def test_unknown_alert_type_falls_back_to_generic(self):
        alert = _make_alert(
            alert_type="optimization_failed",
            title="Optimization timed out",
            detail={"depot_id": "abc", "solver": "gurobi", "elapsed_s": 60.5},
        )
        msg = render_alert(alert, recipient_email="x@x.com", from_address="from@x.com")
        assert "Optimization timed out" in msg.html
        assert "depot_id" in msg.html
        assert "abc" in msg.html
        assert "gurobi" in msg.text


class TestAutoescaping:
    def test_user_supplied_detail_is_escaped_in_html(self):
        evil = {
            "station_id": "<script>alert(1)</script>",
            "connector_id": 1,
            "status": "Faulted",
            "error_code": None,
        }
        alert = _make_alert(detail=evil)
        msg = render_alert(alert, recipient_email="x@x.com", from_address="from@x.com")

        assert "<script>" not in msg.html
        assert "&lt;script&gt;" in msg.html

    def test_text_body_does_not_escape_html(self):
        """Plain-text body keeps the literal characters; the HTML body is the
        attack surface and that one is autoescaped."""
        evil = {
            "station_id": "<<weird>>",
            "connector_id": 1,
            "status": "Faulted",
            "error_code": None,
        }
        alert = _make_alert(detail=evil)
        msg = render_alert(alert, recipient_email="x@x.com", from_address="from@x.com")
        assert "<<weird>>" in msg.text


class TestStrictUndefined:
    """Templates must not silently render empty strings for missing fields —
    the StrictUndefined setting catches typos in templates early."""

    def test_renders_alert_with_complete_fields(self):
        # Sanity: a fully-populated alert renders without UndefinedError.
        msg = render_alert(_make_alert(), recipient_email="x@x.com", from_address="from@x.com")
        assert msg.html
        assert msg.text
        assert msg.subject
