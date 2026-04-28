from __future__ import annotations

import datetime as _dt
from io import BytesIO

from openpyxl import load_workbook

from mcp_office_reader.writer import generate_xlsx
from mcp_office_reader.writer_schema import WorkbookSpec


def _open(b: bytes):
    return load_workbook(BytesIO(b))


def test_minimal_workbook_values_and_header():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S1",
            "columns": [
                {"name": "Name", "type": "text"},
                {"name": "N",    "type": "integer"},
            ],
            "rows": [["Alice", 30], ["Bob", 25]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    ws = wb["S1"]
    assert ws.cell(1, 1).value == "Name"
    assert ws.cell(1, 2).value == "N"
    assert ws.cell(1, 1).font.bold is True
    assert ws.cell(2, 1).value == "Alice"
    assert ws.cell(2, 2).value == 30
    assert isinstance(ws.cell(2, 2).value, int)
    assert ws.cell(3, 2).value == 25


def test_dates_written_as_native_dates():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "D",  "type": "date",     "format": "YYYY-MM-DD"},
                {"name": "DT", "type": "datetime", "format": "YYYY-MM-DD HH:MM"},
            ],
            "rows": [["2026-04-20", "2026-04-20T12:30:00"]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    ws = wb["S"]
    # openpyxl always returns datetime on read (Excel has no date-only type);
    # we check the .date() portion equals the input, and that the value is a
    # native temporal, not a string.
    v = ws.cell(2, 1).value
    assert isinstance(v, _dt.datetime)
    assert v.date() == _dt.date(2026, 4, 20)
    assert ws.cell(2, 2).value == _dt.datetime(2026, 4, 20, 12, 30, 0)
    # format string present
    assert "YYYY" in ws.cell(2, 1).number_format.upper()


def test_number_format_applied():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "V", "type": "number", "format": "#,##0.00"}],
            "rows": [[12345.678]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].cell(2, 1).number_format == "#,##0.00"


def test_default_format_for_currency_and_percent():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "C", "type": "currency"},
                {"name": "P", "type": "percent"},
            ],
            "rows": [[1234.5, 0.125]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert "0" in wb["S"].cell(2, 1).number_format  # some numeric format
    assert "%" in wb["S"].cell(2, 2).number_format


def test_manual_column_width():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "A", "type": "text", "width": 30},
                {"name": "B", "type": "text", "width": "auto"},
            ],
            "rows": [["short", "longer-text-value"]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    ws = wb["S"]
    assert ws.column_dimensions["A"].width == 30


def test_freeze_header_true_by_default():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].freeze_panes == "A2"


def test_freeze_header_false():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "freeze_header": False,
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].freeze_panes is None


def test_auto_filter():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}, {"name": "B", "type": "number"}],
            "rows": [["x", 1]],
            "auto_filter": True,
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].auto_filter.ref == "A1:B2"


def test_thin_borders_on_data():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    from openpyxl.styles.borders import Border
    b: Border = wb["S"].cell(2, 1).border
    assert b.left.style is not None
    assert b.right.style is not None


def test_merges():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "A", "type": "text"},
                {"name": "B", "type": "text"},
                {"name": "C", "type": "text"},
            ],
            "rows": [["x", "y", "z"], ["1", "2", "3"]],
            "merges": ["A1:C1"],
        }]
    })
    wb = _open(generate_xlsx(spec))
    refs = {str(mr) for mr in wb["S"].merged_cells.ranges}
    assert "A1:C1" in refs


def test_cell_styles_bg_fg_bold_single_and_range():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "A", "type": "text"},
                {"name": "B", "type": "text"},
                {"name": "C", "type": "text"},
            ],
            "rows": [["x", "y", "z"]],
            "cell_styles": [
                {"range": "A1:C1", "bg": "DDEBF7", "bold": True},
                {"range": "B2",    "bg": "FFC7CE", "fg": "9C0006"},
            ],
        }]
    })
    wb = _open(generate_xlsx(spec))
    ws = wb["S"]
    # Header row all three cells: bg + bold
    for col in (1, 2, 3):
        c = ws.cell(1, col)
        assert c.fill.fgColor.rgb.endswith("DDEBF7")
        assert c.font.bold is True
    # B2 fg+bg
    b2 = ws.cell(2, 2)
    assert b2.fill.fgColor.rgb.endswith("FFC7CE")
    assert b2.font.color.rgb.endswith("9C0006")


def test_cell_styles_preserve_other_font_attrs():
    from openpyxl import Workbook
    from openpyxl.styles import Font

    from mcp_office_reader.writer import _apply_cell_styles
    from mcp_office_reader.writer_schema import SheetSpec

    wb = Workbook()
    ws = wb.active
    ws.cell(1, 1, value="x").font = Font(
        italic=True, underline="single", strike=True, name="Arial", size=14
    )
    spec = SheetSpec.model_validate({
        "name": "S",
        "columns": [{"name": "A", "type": "text"}],
        "rows": [["x"]],
        "cell_styles": [{"range": "A1", "bold": True, "bg": "CCCCCC"}],
    })
    _apply_cell_styles(ws, spec)
    f = ws.cell(1, 1).font
    assert f.italic is True
    assert f.underline == "single"
    assert f.strike is True
    assert f.name == "Arial"
    assert f.size == 14
    assert f.bold is True  # overridden


def test_multi_sheet_preserves_order_and_values():
    spec = WorkbookSpec.model_validate({
        "sheets": [
            {"name": "A", "columns": [{"name": "x", "type": "integer"}], "rows": [[1]]},
            {"name": "B", "columns": [{"name": "y", "type": "integer"}], "rows": [[2]]},
            {"name": "C", "columns": [{"name": "z", "type": "integer"}], "rows": [[3]]},
        ]
    })
    wb = _open(generate_xlsx(spec))
    assert wb.sheetnames == ["A", "B", "C"]
    assert wb["A"].cell(2, 1).value == 1
    assert wb["B"].cell(2, 1).value == 2
    assert wb["C"].cell(2, 1).value == 3


def test_formula_cell_renders_as_formula_not_text():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "A", "type": "integer"},
                {"name": "B", "type": "integer"},
                {"name": "Total", "type": "formula"},
            ],
            "rows": [
                [10, 20, "=A2+B2"],
                [5,  7,  "=A3+B3"],
            ],
        }]
    })
    wb = _open(generate_xlsx(spec))
    ws = wb["S"]
    assert ws.cell(2, 3).value == "=A2+B2"
    assert ws.cell(2, 3).data_type == "f"
    assert ws.cell(3, 3).value == "=A3+B3"
    assert ws.cell(3, 3).data_type == "f"


def test_formula_column_does_not_apply_default_number_format():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "Total", "type": "formula"}],
            "rows": [["=1+1"]],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].cell(2, 1).number_format == "General"


def test_cross_sheet_formula_round_trip():
    spec = WorkbookSpec.model_validate({
        "sheets": [
            {
                "name": "Data",
                "columns": [{"name": "X", "type": "integer"}],
                "rows": [[100], [200]],
            },
            {
                "name": "Summary",
                "columns": [{"name": "FromData", "type": "formula"}],
                "rows": [["=Data!A2"]],
            },
        ]
    })
    wb = _open(generate_xlsx(spec))
    cell = wb["Summary"].cell(2, 1)
    assert cell.value == "=Data!A2"
    assert cell.data_type == "f"


def test_cell_style_italic_applied():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [{"range": "A2", "italic": True}],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].cell(2, 1).font.italic is True


def test_cell_style_font_size_applied():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [{"range": "A2", "font_size": 14}],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].cell(2, 1).font.size == 14


def test_cell_style_font_name_applied():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [{"range": "A2", "font_name": "Arial"}],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].cell(2, 1).font.name == "Arial"


def test_cell_style_combined_font_attrs_preserve_existing():
    """Setting font_size should not clobber bold from another cell_style on the same cell."""
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [
                {"range": "A2", "bold": True},
                {"range": "A2", "font_size": 14},
            ],
        }]
    })
    wb = _open(generate_xlsx(spec))
    f = wb["S"].cell(2, 1).font
    assert f.bold is True
    assert f.size == 14


def test_cell_style_align_applied():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [{"range": "A2", "align": "center"}],
        }]
    })
    wb = _open(generate_xlsx(spec))
    assert wb["S"].cell(2, 1).alignment.horizontal == "center"


def test_cell_style_align_right_on_range():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "A", "type": "text"},
                {"name": "B", "type": "text"},
            ],
            "rows": [["x", "y"], ["1", "2"]],
            "cell_styles": [{"range": "B2:B3", "align": "right"}],
        }]
    })
    wb = _open(generate_xlsx(spec))
    ws = wb["S"]
    assert ws.cell(2, 2).alignment.horizontal == "right"
    assert ws.cell(3, 2).alignment.horizontal == "right"


def test_cell_style_border_all_applies_thin_border_on_all_sides():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [{"range": "A1", "border": "all"}],   # header row
        }]
    })
    wb = _open(generate_xlsx(spec))
    b = wb["S"].cell(1, 1).border
    assert b.left.style is not None
    assert b.right.style is not None
    assert b.top.style is not None
    assert b.bottom.style is not None


def test_cell_style_border_none_clears_data_borders():
    spec = WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "text"}],
            "rows": [["x"]],
            "cell_styles": [{"range": "A2", "border": "none"}],
        }]
    })
    wb = _open(generate_xlsx(spec))
    b = wb["S"].cell(2, 1).border
    # openpyxl may represent "no border" either as side=None or side.style=None
    for side in (b.left, b.right, b.top, b.bottom):
        assert side is None or side.style is None
