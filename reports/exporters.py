"""CSV / Excel / PDF export of tabular data. Reports only READ data; nothing here writes to the database."""
from __future__ import annotations

import csv
from pathlib import Path

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe(v):
    """Neutralise spreadsheet formula injection for text cells (names typed by users can start with '=')."""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


def export_rows(path: str | Path, title: str, headers: list[str], rows: list[list], *, subtitle: str = "") -> Path:
    p = Path(path)
    ext = p.suffix.lower()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = [[_safe(c) for c in r] for r in rows]
    if ext == ".csv":
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(clean)
    elif ext == ".xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook(); ws = wb.active; ws.title = title[:31]
        ws.append([title]); ws["A1"].font = Font(bold=True, size=13)
        if subtitle:
            ws.append([subtitle])
        ws.append([])
        ws.append(headers)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        for r in clean:
            ws.append(r)
        for i, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in clean if i - 1 < len(r)]) + 2
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 50)
        wb.save(p)
    elif ext == ".pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        st = getSampleStyleSheet()
        doc = SimpleDocTemplate(str(p), pagesize=landscape(A4) if len(headers) > 5 else A4, title=title)
        cell = st["BodyText"]; cell.fontSize = 8; cell.leading = 10
        data = [[Paragraph(f"<b>{h}</b>", cell) for h in headers]] + [[Paragraph(str(c).replace("&", "&amp;").replace("<", "&lt;"), cell) for c in r] for r in rows]
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story = [Paragraph(title, st["Title"])]
        if subtitle:
            story.append(Paragraph(subtitle, st["Normal"]))
        story += [Spacer(1, 8), t]
        doc.build(story)
    else:
        raise ValueError("Use .csv, .xlsx or .pdf")
    return p
