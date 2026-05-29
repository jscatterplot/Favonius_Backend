"""Unit tests for the ev_vs_diesel_tco report rendering (PDF + CSV + validation)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api.report_pdf import render_report_pdf
from src.api.report_schedule_timing import (
    ScheduleValidationError,
    _validate_kind_and_group_by,
)
from src.api.reports import TCO_CSV_COLUMNS, stream_tco_rows_as_csv

_PS = datetime(2026, 5, 18, tzinfo=timezone.utc)
_PE = datetime(2026, 5, 24, tzinfo=timezone.utc)


def _tco_data():
    return {
        "kind": "ev_vs_diesel_tco",
        "currency": "EUR",
        "diesel_currency": "EUR",
        "diesel_price_eur_per_l": 1.45,
        "diesel_region": "LT",
        "currency_mismatch": False,
        "priceable_vehicle_count": 3,
        "unpriceable_vehicle_count": 1,
        "notes": [],
        "depot_name": "Vilnius Depot",
        "by_vehicle_type": [
            {
                "vehicle_type": "bus_large",
                "vehicle_count": 2,
                "distance_km": 1200.0,
                "ev_energy_kwh": 1500.0,
                "ev_cost": 210.0,
                "ev_eur_per_km": 0.175,
                "diesel_litres": 420.0,
                "diesel_cost": 609.0,
                "diesel_eur_per_km": 0.5075,
                "pct_difference": 65.5,
            },
            {
                "vehicle_type": "van",
                "vehicle_count": 1,
                "distance_km": 300.0,
                "ev_energy_kwh": 60.0,
                "ev_cost": 12.0,
                "ev_eur_per_km": 0.04,
                "diesel_litres": 33.0,
                "diesel_cost": 47.85,
                "diesel_eur_per_km": 0.1595,
                "pct_difference": 74.9,
            },
        ],
        "totals": {
            "distance_km": 1500.0,
            "ev_cost": 222.0,
            "diesel_cost": 656.85,
            "ev_eur_per_km": 0.148,
            "diesel_eur_per_km": 0.4379,
            "pct_difference": 66.2,
        },
    }


# ── PDF ───────────────────────────────────────────────────────────────────────


def test_pdf_renders_for_tco_kind():
    pdf = render_report_pdf(
        title="Weekly EV vs Diesel",
        depot_name="Vilnius Depot",
        kind="ev_vs_diesel_tco",
        period_start=_PS,
        period_end=_PE,
        group_by=None,
        data=_tco_data(),
    )
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000  # non-trivial document with the comparison tables


def test_pdf_empty_tco_data_renders_no_data_message():
    pdf = render_report_pdf(
        title="Weekly EV vs Diesel",
        depot_name="Vilnius Depot",
        kind="ev_vs_diesel_tco",
        period_start=_PS,
        period_end=_PE,
        group_by=None,
        data={"by_vehicle_type": [], "totals": {}, "currency": "EUR"},
    )
    assert pdf[:5] == b"%PDF-"  # still a valid PDF, just the "nothing to compare" page


def test_pdf_none_data_is_cover_page():
    pdf = render_report_pdf(
        title="Weekly EV vs Diesel",
        depot_name="Vilnius Depot",
        kind="ev_vs_diesel_tco",
        period_start=_PS,
        period_end=_PE,
        group_by=None,
        data=None,
    )
    assert pdf[:5] == b"%PDF-"


# ── CSV ───────────────────────────────────────────────────────────────────────


def test_csv_header_rows_and_total():
    text = "".join(stream_tco_rows_as_csv(_tco_data()))
    lines = [ln for ln in text.splitlines() if ln]
    assert lines[0] == ",".join(TCO_CSV_COLUMNS)
    # 2 vehicle-type rows + 1 TOTAL row.
    assert len(lines) == 4
    assert lines[1].startswith("bus_large,2,1200.0")
    assert lines[-1].startswith("TOTAL,")
    assert "66.2" in lines[-1]


def test_csv_unpriceable_cells_blank_not_zero():
    data = {
        "by_vehicle_type": [
            {
                "vehicle_type": "bus",
                "vehicle_count": 1,
                "distance_km": None,
                "ev_energy_kwh": 50.0,
                "ev_cost": 10.0,
                "ev_eur_per_km": None,
                "diesel_litres": None,
                "diesel_cost": None,
                "diesel_eur_per_km": None,
                "pct_difference": None,
            }
        ],
        "totals": {},
    }
    text = "".join(stream_tco_rows_as_csv(data))
    row = text.splitlines()[1]
    # distance_km is the 3rd column → empty between two commas.
    cells = row.split(",")
    assert cells[0] == "bus"
    assert cells[2] == ""  # distance None → blank
    assert cells[5] == ""  # ev_eur_per_km None → blank


def test_csv_empty_data_is_header_only():
    text = "".join(stream_tco_rows_as_csv({"by_vehicle_type": [], "totals": {}}))
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == 1  # header only


# ── Schedule validation ───────────────────────────────────────────────────────


def test_validate_accepts_tco_without_group_by():
    kind, group_by = _validate_kind_and_group_by("ev_vs_diesel_tco", None)
    assert kind == "ev_vs_diesel_tco"
    assert group_by is None


def test_validate_rejects_tco_with_group_by():
    with pytest.raises(ScheduleValidationError):
        _validate_kind_and_group_by("ev_vs_diesel_tco", "vehicle")
