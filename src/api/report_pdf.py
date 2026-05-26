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
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if totals is not None:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        style.append(("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#1f2937")))
    table.setStyle(TableStyle(style))
    return table


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
