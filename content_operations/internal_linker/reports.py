"""
Phase 3 — export 'new' suggestions to an Excel workbook, one tab per detector
plus a combined ranked sheet, so the content team has a working queue.

The DB table is the source of truth; this is a convenience export. Writers can
mark suggestions 'applied' or 'ignored' directly in the DB (or via a future
UI); the Excel is regenerated each run and reflects whatever the DB holds at
export time.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from common.database import fetch_all

logger = logging.getLogger("content_operations.internal_linker.reports")

DETECTORS = [
    ("striking_distance_support", "Striking Distance Support"),
    ("blog_to_money_page",        "Blog -> Money Page"),
]

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF")
BAND_FILL   = PatternFill("solid", fgColor="F2F2F2")
HOT_FILL    = PatternFill("solid", fgColor="D6F5D6")

COLS = ["Priority", "From URL", "To URL", "Suggested Anchor",
        "Status", "Reason", "First Seen", "Last Seen"]


def _write_header(ws) -> None:
    for i, name in enumerate(COLS, 1):
        c = ws.cell(1, i, name)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    widths = [10, 60, 60, 36, 10, 70, 12, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def _write_rows(ws, rows) -> None:
    for i, r in enumerate(rows, 2):
        prio = r["priority_score"]
        ws.cell(i, 1, prio)
        ws.cell(i, 2, r["from_url"])
        ws.cell(i, 3, r["to_url"])
        ws.cell(i, 4, r["suggested_anchor"] or "")
        ws.cell(i, 5, r["status"])
        ws.cell(i, 6, r["reason"])
        ws.cell(i, 7, r["first_suggested_at"].date().isoformat()
                      if r.get("first_suggested_at") else "")
        ws.cell(i, 8, r["last_suggested_at"].date().isoformat()
                      if r.get("last_suggested_at") else "")
        # Zebra band
        if i % 2 == 0:
            for col in range(1, len(COLS) + 1):
                if ws.cell(i, col).fill.start_color.index == "00000000":
                    ws.cell(i, col).fill = BAND_FILL
        # Highlight the top decile by priority
        if prio and prio >= 100:
            ws.cell(i, 1).fill = HOT_FILL


def run(output_path: Path | None = None,
        include_statuses: tuple[str, ...] = ("new",)) -> dict:
    """
    Generate the Excel. Defaults to only rows the writer hasn't touched yet
    ('new'); pass include_statuses=('new','stale','applied','ignored') to get
    the full audit view.
    """
    if output_path is None:
        default_dir = Path(__file__).resolve().parents[2] / "outputs" / "internal_linker"
        default_dir.mkdir(parents=True, exist_ok=True)
        output_path = default_dir / f"internal_link_suggestions_{date.today().isoformat()}.xlsx"

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # 1. Summary
    summary = wb.create_sheet("Summary")
    for i, name in enumerate(["Detector", "New", "Applied", "Ignored", "Stale", "Total"], 1):
        c = summary.cell(1, i, name); c.fill = HEADER_FILL; c.font = HEADER_FONT
    row_i = 2
    for det, pretty in DETECTORS:
        counts = {s: 0 for s in ("new", "applied", "ignored", "stale")}
        for r in fetch_all(
            "SELECT status, count(*) n FROM internal_link_suggestions "
            "WHERE detector = %s GROUP BY status", [det],
        ):
            counts[r["status"]] = r["n"]
        total = sum(counts.values())
        summary.cell(row_i, 1, pretty)
        summary.cell(row_i, 2, counts["new"])
        summary.cell(row_i, 3, counts["applied"])
        summary.cell(row_i, 4, counts["ignored"])
        summary.cell(row_i, 5, counts["stale"])
        summary.cell(row_i, 6, total)
        row_i += 1
    for col, w in zip("ABCDEF", [30, 10, 10, 10, 10, 10]):
        summary.column_dimensions[col].width = w
    summary.freeze_panes = "A2"

    # 2. Per-detector tabs
    totals = {}
    for det, pretty in DETECTORS:
        placeholders = ",".join(["%s"] * len(include_statuses))
        rows = fetch_all(
            f"""
            SELECT from_url, to_url, suggested_anchor, reason,
                   priority_score, status,
                   first_suggested_at, last_suggested_at
              FROM internal_link_suggestions
             WHERE detector = %s AND status IN ({placeholders})
             ORDER BY priority_score DESC, from_url, to_url
            """,
            [det, *include_statuses],
        )
        ws = wb.create_sheet(pretty[:31])
        _write_header(ws)
        _write_rows(ws, rows)
        if rows:
            ws.auto_filter.ref = ws.dimensions
        totals[det] = len(rows)

    # 3. Combined, ranked across both detectors
    placeholders = ",".join(["%s"] * len(include_statuses))
    all_rows = fetch_all(
        f"""
        SELECT detector, from_url, to_url, suggested_anchor, reason,
               priority_score, status,
               first_suggested_at, last_suggested_at
          FROM internal_link_suggestions
         WHERE status IN ({placeholders})
         ORDER BY priority_score DESC, detector, from_url
        """,
        list(include_statuses),
    )
    ws = wb.create_sheet("All (ranked)")
    cols = ["Detector"] + COLS
    for i, name in enumerate(cols, 1):
        c = ws.cell(1, i, name); c.fill = HEADER_FILL; c.font = HEADER_FONT
    widths = [24, 10, 60, 60, 36, 10, 70, 12, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    for i, r in enumerate(all_rows, 2):
        ws.cell(i, 1, r["detector"])
        ws.cell(i, 2, r["priority_score"])
        ws.cell(i, 3, r["from_url"])
        ws.cell(i, 4, r["to_url"])
        ws.cell(i, 5, r["suggested_anchor"] or "")
        ws.cell(i, 6, r["status"])
        ws.cell(i, 7, r["reason"])
        ws.cell(i, 8, r["first_suggested_at"].date().isoformat()
                      if r.get("first_suggested_at") else "")
        ws.cell(i, 9, r["last_suggested_at"].date().isoformat()
                      if r.get("last_suggested_at") else "")
        if i % 2 == 0:
            for col in range(1, len(cols) + 1):
                if ws.cell(i, col).fill.start_color.index == "00000000":
                    ws.cell(i, col).fill = BAND_FILL
        if r["priority_score"] and r["priority_score"] >= 100:
            ws.cell(i, 2).fill = HOT_FILL
    if all_rows:
        ws.auto_filter.ref = ws.dimensions

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    logger.info("wrote %s", output_path)
    return {
        "output_path":    str(output_path),
        "per_detector":   totals,
        "combined_rows":  len(all_rows),
    }
