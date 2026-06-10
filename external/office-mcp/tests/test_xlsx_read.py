from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from office_mcp.analytics import load_table, summarize_dataframe
from office_mcp.reader import _convert_with_markitdown
from office_mcp.writer import generate_xlsx
from office_mcp.writer_schema import WorkbookSpec
from office_mcp.xlsx_read import load_xlsx_table, xlsx_to_markdown


def _formula_workbook_spec() -> WorkbookSpec:
    return WorkbookSpec.model_validate({
        "sheets": [{
            "name": "S",
            "columns": [
                {"name": "A", "type": "integer"},
                {"name": "B", "type": "integer"},
                {"name": "Total", "type": "formula"},
            ],
            "rows": [
                [10, 20, "=A2+B2"],
                [5, 7, "=A3+B3"],
            ],
        }]
    })


@pytest.fixture
def formula_xlsx(tmp_path: Path) -> Path:
    path = tmp_path / "formula.xlsx"
    path.write_bytes(generate_xlsx(_formula_workbook_spec()))
    return path


def test_load_xlsx_table_shows_formula_text_without_cache(formula_xlsx: Path):
    df = load_xlsx_table(formula_xlsx)
    assert df["Total"].tolist() == ["=A2+B2", "=A3+B3"]


def test_load_table_uses_formula_fallback_for_xlsx(formula_xlsx: Path):
    df = load_table(formula_xlsx)
    assert df["Total"].tolist() == ["=A2+B2", "=A3+B3"]


def test_inspect_summary_shows_formula_text(formula_xlsx: Path):
    summary = summarize_dataframe(load_table(formula_xlsx))
    assert summary["head"][0][2] == "=A2+B2"
    assert summary["head"][1][2] == "=A3+B3"
    total_col = next(c for c in summary["columns"] if c["name"] == "Total")
    assert total_col["dtype"] == "text"
    assert "=A2+B2" in total_col["sample"]


def test_xlsx_to_markdown_shows_formula_text(formula_xlsx: Path):
    md = xlsx_to_markdown(formula_xlsx)
    assert "=A2+B2" in md
    assert "=A3+B3" in md
    assert "NaN" not in md


def test_read_file_xlsx_uses_formula_text(formula_xlsx: Path):
    md = _convert_with_markitdown(formula_xlsx)
    assert "=A2+B2" in md
    assert "NaN" not in md


def test_cached_values_are_not_replaced(tmp_path: Path):
    """When pandas already read a cached/computed number, keep it."""
    path = tmp_path / "cached.xlsx"
    pd.DataFrame({"A": [10], "B": [20], "Total": [30]}).to_excel(path, index=False)
    df = load_xlsx_table(path)
    assert df["Total"].tolist() == [30]


def test_plain_xlsx_unchanged(sample_xlsx: Path):
    df_before = pd.read_excel(sample_xlsx)
    df_after = load_xlsx_table(sample_xlsx)
    pd.testing.assert_frame_equal(df_before, df_after)


def test_pandas_sample_xlsx_unchanged(tmp_path: Path):
    path = tmp_path / "data.xlsx"
    pd.DataFrame({
        "id": [1, 2, 3],
        "status": ["open", "closed", "open"],
        "hours": [4.5, 8.0, 2.0],
    }).to_excel(path, index=False, sheet_name="Sales")
    df = load_table(path)
    assert list(df.columns) == ["id", "status", "hours"]
    assert len(df) == 3
