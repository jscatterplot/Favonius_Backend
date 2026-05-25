"""Unit tests for the report PDF renderer (reportlab)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.api.report_pdf import render_report_pdf


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _consumption_data():
    return {
        "group_by": "card",
        "depot_name": "HRX Depot",
        "currency": "EUR",
        "rows": [
            {
                "bucket": "2026-05",
                "card_id": "card-1",
                "card_label": "Bus 12",
                "energy_kwh": 123.45,
                "session_count": 8,
                "avg_kw": 42.1,
                "cost": {"amount": 30.5, "currency": "EUR", "estimated": False},
            },
            {
                "bucket": "2026-05",
                "card_id": "card-2",
                "card_label": "Bus 13",
                "energy_kwh": 67.8,
                "session_count": 4,
                "avg_kw": 38.0,
                "cost": {"amount": 16.0, "currency": "EUR", "estimated": True},
            },
        ],
        "totals": {
            "energy_kwh": 191.25,
            "session_count": 12,
            "cost": {"amount": 46.5, "currency": "EUR", "estimated": True},
        },
    }


def test_render_consumption_pdf_is_valid_pdf():
    pdf = render_report_pdf(
        title="Monthly electricity consumption — May 2026",
        depot_name="HRX Depot",
        kind="monthly_consumption",
        period_start=_dt(2026, 5, 1),
        period_end=_dt(2026, 5, 31),
        group_by="card",
        data=_consumption_data(),
    )
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF")
    assert pdf.rstrip().endswith(b"%%EOF")
    # A table with two data rows + totals should be non-trivial in size.
    assert len(pdf) > 1500


def test_render_pdf_without_data_still_valid():
    pdf = render_report_pdf(
        title="Weekly ops",
        depot_name="HRX Depot",
        kind="weekly_ops",
        period_start=_dt(2026, 5, 18),
        period_end=_dt(2026, 5, 24),
        group_by=None,
        data=None,
    )
    assert pdf.startswith(b"%PDF")


def test_render_pdf_vehicle_grouping():
    data = {
        "group_by": "vehicle",
        "rows": [
            {
                "bucket": "2026-05",
                "vehicle_id": "veh-1",
                "energy_kwh": 10.0,
                "session_count": 1,
                "avg_kw": 10.0,
                "cost": {"amount": 2.5, "currency": "USD", "estimated": False},
            }
        ],
        "totals": None,
    }
    pdf = render_report_pdf(
        title="Vehicle consumption",
        depot_name="Depot",
        kind="monthly_consumption",
        period_start=_dt(2026, 5, 1),
        period_end=_dt(2026, 5, 31),
        group_by="vehicle",
        data=data,
    )
    assert pdf.startswith(b"%PDF")
