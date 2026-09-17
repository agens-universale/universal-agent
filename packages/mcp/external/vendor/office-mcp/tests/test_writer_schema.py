import datetime as _dt

import pytest
from pydantic import ValidationError

from office_mcp.writer_schema import WorkbookSpec


def _minimal() -> dict:
    return {
        "sheets": [
            {
                "name": "S1",
                "columns": [{"name": "A", "type": "text"}],
                "rows": [["x"]],
            }
        ]
    }


def test_minimal_valid():
    WorkbookSpec.model_validate(_minimal())


def test_sheets_must_be_nonempty():
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate({"sheets": []})


def test_row_length_mismatch():
    d = _minimal()
    d["sheets"][0]["rows"] = [["x", "y"]]
    with pytest.raises(ValidationError, match="row 0"):
        WorkbookSpec.model_validate(d)


def test_duplicate_sheet_name():
    d = _minimal()
    d["sheets"].append(d["sheets"][0])
    with pytest.raises(ValidationError, match="duplicate"):
        WorkbookSpec.model_validate(d)


def test_sheet_name_too_long():
    d = _minimal()
    d["sheets"][0]["name"] = "x" * 32
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate(d)


@pytest.mark.parametrize("bad_name", ["a:b", "a/b", "a\\b", "a?b", "a*b", "a[b", "a]b", ""])
def test_sheet_name_forbidden_chars(bad_name):
    d = _minimal()
    d["sheets"][0]["name"] = bad_name
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate(d)


def test_number_type_accepts_int_and_float():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "n", "type": "number"}]
    d["sheets"][0]["rows"] = [[1], [1.5]]
    WorkbookSpec.model_validate(d)


def test_number_rejects_string():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "n", "type": "number"}]
    d["sheets"][0]["rows"] = [["not-a-number"]]
    with pytest.raises(ValidationError, match="number"):
        WorkbookSpec.model_validate(d)


def test_date_iso_parsed():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "d", "type": "date"}]
    d["sheets"][0]["rows"] = [["2026-04-20"]]
    spec = WorkbookSpec.model_validate(d)
    assert spec.sheets[0].coerced_rows()[0][0] == _dt.date(2026, 4, 20)


def test_date_rejects_non_iso():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "d", "type": "date"}]
    d["sheets"][0]["rows"] = [["20/04/2026"]]
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate(d)


def test_coerced_rows_rejects_mutated_bad_iso_date():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "d", "type": "date"}]
    d["sheets"][0]["rows"] = [["2026-04-20"]]
    spec = WorkbookSpec.model_validate(d)
    spec.sheets[0].rows[0][0] = "not-a-date"
    with pytest.raises(ValueError, match="bad ISO"):
        spec.sheets[0].coerced_rows()


def test_null_allowed_in_any_type():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "n", "type": "number"}]
    d["sheets"][0]["rows"] = [[None]]
    WorkbookSpec.model_validate(d)


def test_merge_out_of_bounds():
    d = _minimal()
    d["sheets"][0]["merges"] = ["A1:Z100"]
    with pytest.raises(ValidationError, match="out of bounds"):
        WorkbookSpec.model_validate(d)


def test_merge_invalid_range():
    d = _minimal()
    d["sheets"][0]["merges"] = ["not-a-range"]
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate(d)


def test_cell_style_bad_color():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [{"range": "A1", "bg": "red"}]
    with pytest.raises(ValidationError, match="hex"):
        WorkbookSpec.model_validate(d)


def test_cell_style_ok():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [
        {"range": "A1", "bg": "FFC7CE", "fg": "9C0006", "bold": True}
    ]
    WorkbookSpec.model_validate(d)


def test_format_empty_string_rejected():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "V", "type": "number", "format": ""}]
    d["sheets"][0]["rows"] = [[1]]
    with pytest.raises(ValidationError, match="format"):
        WorkbookSpec.model_validate(d)


def test_formula_type_accepts_value_starting_with_eq():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "Total", "type": "formula"}]
    d["sheets"][0]["rows"] = [["=SUM(A1:A10)"]]
    spec = WorkbookSpec.model_validate(d)
    assert spec.sheets[0].rows[0][0] == "=SUM(A1:A10)"


def test_formula_type_rejects_value_without_eq():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "Total", "type": "formula"}]
    d["sheets"][0]["rows"] = [["SUM(A1:A10)"]]
    with pytest.raises(ValidationError, match="formula"):
        WorkbookSpec.model_validate(d)


def test_formula_type_rejects_non_string_value():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "Total", "type": "formula"}]
    d["sheets"][0]["rows"] = [[42]]
    with pytest.raises(ValidationError, match="formula"):
        WorkbookSpec.model_validate(d)


def test_formula_type_allows_null():
    d = _minimal()
    d["sheets"][0]["columns"] = [{"name": "Total", "type": "formula"}]
    d["sheets"][0]["rows"] = [[None]]
    WorkbookSpec.model_validate(d)


def test_cell_style_accepts_extended_fields():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [{
        "range": "A1",
        "italic": True,
        "align": "center",
        "font_size": 14,
        "font_name": "Arial",
        "border": "all",
    }]
    spec = WorkbookSpec.model_validate(d)
    cs = spec.sheets[0].cell_styles[0]
    assert cs.italic is True
    assert cs.align == "center"
    assert cs.font_size == 14
    assert cs.font_name == "Arial"
    assert cs.border == "all"


def test_cell_style_font_size_must_be_positive():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [{"range": "A1", "font_size": 0}]
    with pytest.raises(ValidationError, match="font_size"):
        WorkbookSpec.model_validate(d)


def test_cell_style_font_name_must_be_nonempty():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [{"range": "A1", "font_name": ""}]
    with pytest.raises(ValidationError, match="font_name"):
        WorkbookSpec.model_validate(d)


def test_cell_style_align_rejects_unknown_value():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [{"range": "A1", "align": "justify"}]
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate(d)


def test_cell_style_border_rejects_unknown_value():
    d = _minimal()
    d["sheets"][0]["cell_styles"] = [{"range": "A1", "border": "thick"}]
    with pytest.raises(ValidationError):
        WorkbookSpec.model_validate(d)
