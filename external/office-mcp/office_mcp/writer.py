from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .writer_a1 import parse_a1_range
from .writer_md_to_docx import md_to_docx_bytes as generate_docx  # noqa: F401
from .writer_schema import SheetSpec, WorkbookSpec

_DEFAULT_FORMATS: dict[str, str] = {
    "date":     "yyyy-mm-dd",
    "datetime": "yyyy-mm-dd hh:mm:ss",
    "currency": "#,##0.00",
    "percent":  "0.00%",
}

_THIN = Side(border_style="thin", color="CCCCCC")
_DATA_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def generate_xlsx(spec: WorkbookSpec) -> bytes:
    wb = Workbook()
    # openpyxl creates a default sheet; remove it, we'll add our own
    default = wb.active
    wb.remove(default)

    for sheet_spec in spec.sheets:
        ws = wb.create_sheet(sheet_spec.name)
        _write_header(ws, sheet_spec)
        _write_rows(ws, sheet_spec)
        _apply_column_widths(ws, sheet_spec)
        _apply_freeze_and_filter(ws, sheet_spec)
        _apply_data_borders(ws, sheet_spec)
        _apply_merges(ws, sheet_spec)
        _apply_cell_styles(ws, sheet_spec)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_header(ws, sheet_spec: SheetSpec) -> None:
    bold = Font(bold=True)
    for j, col in enumerate(sheet_spec.columns, start=1):
        cell = ws.cell(row=1, column=j, value=col.name)
        cell.font = bold


def _write_rows(ws, sheet_spec: SheetSpec) -> None:
    formats = [c.format or _DEFAULT_FORMATS.get(c.type) for c in sheet_spec.columns]
    for i, row in enumerate(sheet_spec.coerced_rows(), start=2):
        for j, val in enumerate(row, start=1):
            cell = ws.cell(row=i, column=j, value=val)
            if formats[j - 1]:
                cell.number_format = formats[j - 1]


def _apply_column_widths(ws, sheet_spec: SheetSpec) -> None:
    for j, col in enumerate(sheet_spec.columns, start=1):
        letter = get_column_letter(j)
        if isinstance(col.width, (int, float)):
            ws.column_dimensions[letter].width = col.width
        elif col.width == "auto" or col.width is None:
            max_len = len(col.name)
            for row in sheet_spec.rows:
                v = row[j - 1]
                if v is not None:
                    max_len = max(max_len, len(str(v)))
            ws.column_dimensions[letter].width = max(10, min(60, max_len + 2))


def _apply_freeze_and_filter(ws, sheet_spec: SheetSpec) -> None:
    if sheet_spec.freeze_header:
        ws.freeze_panes = "A2"
    if sheet_spec.auto_filter and sheet_spec.rows:
        last_col = get_column_letter(len(sheet_spec.columns))
        last_row = len(sheet_spec.rows) + 1
        ws.auto_filter.ref = f"A1:{last_col}{last_row}"


def _apply_data_borders(ws, sheet_spec: SheetSpec) -> None:
    for i in range(2, 2 + len(sheet_spec.rows)):
        for j in range(1, 1 + len(sheet_spec.columns)):
            ws.cell(i, j).border = _DATA_BORDER


def _apply_merges(ws, sheet_spec: SheetSpec) -> None:
    for r in sheet_spec.merges:
        ws.merge_cells(r)


def _apply_cell_styles(ws, sheet_spec: SheetSpec) -> None:
    for cs in sheet_spec.cell_styles:
        cmin, rmin, cmax, rmax = parse_a1_range(cs.range)
        for row in range(rmin, rmax + 1):
            for col in range(cmin, cmax + 1):
                cell = ws.cell(row, col)
                if cs.bg:
                    cell.fill = PatternFill("solid", fgColor=cs.bg)
                _maybe_update_font(cell, cs)
                if cs.align is not None:
                    cell.alignment = Alignment(horizontal=cs.align)
                if cs.border == "all":
                    cell.border = _DATA_BORDER
                elif cs.border == "none":
                    cell.border = Border()


def _maybe_update_font(cell, cs) -> None:
    touches_font = (
        cs.bold is not None
        or cs.italic is not None
        or cs.fg
        or cs.font_size is not None
        or cs.font_name is not None
    )
    if not touches_font:
        return
    old = cell.font
    cell.font = Font(
        name=cs.font_name if cs.font_name is not None else old.name,
        size=cs.font_size if cs.font_size is not None else old.size,
        italic=cs.italic if cs.italic is not None else old.italic,
        underline=old.underline,
        strike=old.strike,
        vertAlign=old.vertAlign,
        bold=cs.bold if cs.bold is not None else old.bold,
        color=cs.fg if cs.fg else old.color,
    )
