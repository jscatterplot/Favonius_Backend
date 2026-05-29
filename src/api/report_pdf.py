"""Render a persisted report into a PDF byte string.

Isolated in its own module so the only place that depends on ``reportlab`` is
the delivery path, which imports this lazily. The renderer reads the same
aggregated ``reports.data`` JSONB that the CSV export streams, so PDF and CSV
attachments describe identical numbers.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _fmt_period(period_start: Optional[datetime], period_end: Optional[datetime]) -> str:
    if period_start is None or period_end is None:
        return "—"
    return f"{period_start.date().isoformat()} → {period_end.date().isoformat()}"


def _fmt_cost(cost: Any) -> str:
    if not isinstance(cost, dict):
        return "—"
    amount = cost.get("amount")
    currency = cost.get("currency") or ""
    if amount is None:
        return "—"
    suffix = " (est)" if cost.get("estimated") else ""
    return f"{amount:.2f} {currency}{suffix}".strip()


def _base_table_style(*, has_total_row: bool) -> TableStyle:
    """Shared table palette for all report tables (DRY across report kinds).

    Adds bold + a rule above the last row when ``has_total_row`` so a TOTAL/
    fleet-summary row stands out.
    """
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if has_total_row:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        style.append(("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#1f2937")))
    return TableStyle(style)


def _fmt_num(value: Any, digits: int = 2) -> str:
    """Format an optional number to ``digits`` places, or ``—`` when None."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(value: Any) -> str:
    """Format a signed percentage (positive = EV cheaper), or ``—`` when None."""
    if value is None:
        return "—"
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _consumption_table(data: dict) -> Table:
    group_by = data.get("group_by")
    rows = data.get("rows") or []
    totals = data.get("totals")

    if group_by == "card":
        header = ["Bucket", "Card", "Label", "Energy (kWh)", "Sessions", "Avg kW", "Cost"]
    elif group_by == "vehicle":
        header = ["Bucket", "Vehicle", "Energy (kWh)", "Sessions", "Avg kW", "Cost"]
    else:
        header = ["Bucket", "Energy (kWh)", "Sessions", "Avg kW", "Cost"]

    table_data: list[list[str]] = [header]
    for row in rows:
        cells = [str(row.get("bucket", ""))]
        if group_by == "card":
            cells.append(str(row.get("card_id", "")))
            cells.append(str(row.get("card_label", "")))
        elif group_by == "vehicle":
            cells.append(str(row.get("vehicle_id", "")))
        cells.extend(
            [
                f"{row.get('energy_kwh', 0):.2f}",
                str(row.get("session_count", 0)),
                f"{row.get('avg_kw', 0):.2f}",
                _fmt_cost(row.get("cost")),
            ]
        )
        table_data.append(cells)

    if totals is not None:
        total_cells = ["TOTAL"]
        # Pad the grouping columns so totals align under the numeric columns.
        if group_by == "card":
            total_cells.extend(["", ""])
        elif group_by == "vehicle":
            total_cells.append("")
        total_cells.extend(
            [
                f"{totals.get('energy_kwh', 0):.2f}",
                str(totals.get("session_count", 0)),
                "",
                _fmt_cost(totals.get("cost")),
            ]
        )
        table_data.append(total_cells)

    table = Table(table_data, repeatRows=1)
    table.setStyle(_base_table_style(has_total_row=totals is not None))
    return table


def _tco_story(data: dict, styles: Any) -> list[Any]:
    """Build the EV-vs-diesel comparison flowables (headline + two tables).

    ``data`` is the JSON stored by serialize_fleet_km_result: a ``by_vehicle_type``
    list, a ``totals`` dict, and the diesel context. Unknown / unpriceable cells
    render as ``—``; a headline paragraph states the fleet savings when positive.
    """
    flowables: list[Any] = []
    totals = data.get("totals") or {}
    currency = data.get("currency") or ""
    fleet_pct = totals.get("pct_difference")

    # Headline.
    if isinstance(fleet_pct, (int, float)):
        if fleet_pct > 0:
            headline = (
                f"Electrifying this fleet is {fleet_pct:.1f}% cheaper per kilometre "
                f"than diesel."
            )
        elif fleet_pct < 0:
            headline = (
                f"This fleet is currently {abs(fleet_pct):.1f}% more expensive per "
                f"kilometre than diesel."
            )
        else:
            headline = "This fleet's per-kilometre cost matches diesel."
        flowables.append(Paragraph(headline, styles["Heading2"]))
        flowables.append(Spacer(1, 4 * mm))

    region = data.get("diesel_region")
    price = data.get("diesel_price_eur_per_l")
    flowables.append(
        Paragraph(
            f"Wholesale diesel price: {_fmt_num(price, 3)} EUR/L"
            + (f" ({region})" if region else ""),
            styles["Normal"],
        )
    )
    if data.get("currency_mismatch"):
        flowables.append(
            Paragraph(
                f"⚠ EV cost is in {currency} but diesel is priced in EUR — the "
                f"per-km comparison mixes currencies.",
                styles["Italic"],
            )
        )
    flowables.append(Spacer(1, 6 * mm))

    by_type = data.get("by_vehicle_type") or []
    if not by_type:
        flowables.append(
            Paragraph(
                "No vehicles with both measured distance and a diesel price in "
                "this period — nothing to compare.",
                styles["Italic"],
            )
        )
        return flowables

    # Per-vehicle-type table.
    header = [
        "Vehicle type",
        "Vehicles",
        "Distance (km)",
        "EV kWh",
        f"EV {currency}/km",
        "Diesel L/100km*",
        "Diesel EUR/km",
        "Δ % saved",
    ]
    table_data: list[list[str]] = [header]
    for a in by_type:
        # Derive the implied L/100km for display (litres / distance * 100).
        dist = a.get("distance_km")
        litres = a.get("diesel_litres")
        l_per_100 = (litres / dist * 100.0) if dist and litres is not None else None
        table_data.append(
            [
                str(a.get("vehicle_type", "")),
                str(a.get("vehicle_count", 0)),
                _fmt_num(a.get("distance_km"), 1),
                _fmt_num(a.get("ev_energy_kwh"), 1),
                _fmt_num(a.get("ev_eur_per_km"), 4),
                _fmt_num(l_per_100, 1),
                _fmt_num(a.get("diesel_eur_per_km"), 4),
                _fmt_pct(a.get("pct_difference")),
            ]
        )
    type_table = Table(table_data, repeatRows=1)
    type_table.setStyle(_base_table_style(has_total_row=False))
    flowables.append(type_table)
    flowables.append(Spacer(1, 6 * mm))

    # Fleet summary table.
    summary_header = [
        f"Fleet EV {currency}/km",
        "Fleet diesel EUR/km",
        "Savings %",
        "Total distance (km)",
    ]
    summary_row = [
        _fmt_num(totals.get("ev_eur_per_km"), 4),
        _fmt_num(totals.get("diesel_eur_per_km"), 4),
        _fmt_pct(totals.get("pct_difference")),
        _fmt_num(totals.get("distance_km"), 1),
    ]
    summary_table = Table([summary_header, summary_row], repeatRows=1)
    summary_table.setStyle(_base_table_style(has_total_row=False))
    flowables.append(summary_table)
    flowables.append(Spacer(1, 3 * mm))
    flowables.append(
        Paragraph(
            "* Diesel litres/100km are configured per-vehicle-type baselines for "
            "an equivalent diesel vehicle; EV figures are measured.",
            styles["Italic"],
        )
    )
    return flowables


def render_report_pdf(
    *,
    title: str,
    depot_name: str,
    kind: str,
    period_start: Optional[datetime],
    period_end: Optional[datetime],
    group_by: Optional[str],
    data: Optional[dict],
) -> bytes:
    """Render a report PDF and return the raw bytes.

    Always returns a valid single- or multi-page PDF. For ``monthly_consumption``
    with stored aggregation data a table is rendered; other kinds (or empty data)
    produce a metadata-only cover page.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph(title, styles["Title"]),
        Spacer(1, 4 * mm),
        Paragraph(f"Depot: {depot_name}", styles["Normal"]),
        Paragraph(f"Period: {_fmt_period(period_start, period_end)}", styles["Normal"]),
        Paragraph(f"Report type: {kind}", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    if kind == "monthly_consumption" and isinstance(data, dict) and data.get("rows"):
        if group_by:
            story.append(Paragraph(f"Grouped by: {group_by}", styles["Normal"]))
            story.append(Spacer(1, 4 * mm))
        story.append(_consumption_table(data))
    elif kind == "ev_vs_diesel_tco" and isinstance(data, dict):
        story.extend(_tco_story(data, styles))
    else:
        story.append(
            Paragraph(
                "No tabular data is available for this report.",
                styles["Italic"],
            )
        )

    doc.build(story)
    return buffer.getvalue()


__all__ = ["render_report_pdf"]
