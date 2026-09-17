from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import load_workbook


def _resolve_sheet_name(path: Path, sheet: str | int | None) -> str:
    with pd.ExcelFile(path) as xf:
        if sheet is None or sheet == 0:
            return xf.sheet_names[0]
        if isinstance(sheet, int):
            return xf.sheet_names[sheet]
        if sheet not in xf.sheet_names:
            raise ValueError(f"sheet {sheet!r} not found")
        return sheet


def _cell_has_formula(cell) -> bool:
    if getattr(cell, "data_type", None) == "f":
        return True
    value = cell.value
    return isinstance(value, str) and value.startswith("=")


def _fill_formula_text(df: pd.DataFrame, path: Path, sheet_name: str) -> pd.DataFrame:
    """Replace NaN with formula text where the cell has a formula but no cached value."""
    if not df.isna().any().any():
        return df

    out = df.copy()
    pending: list[tuple[int, int, str]] = []
    cols_to_object: set[int] = set()
    wb = load_workbook(path, data_only=False, read_only=True)
    try:
        ws = wb[sheet_name]
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=False)):
            if row_idx >= len(out):
                break
            for col_idx, cell in enumerate(row):
                if col_idx >= len(out.columns):
                    break
                if pd.isna(out.iloc[row_idx, col_idx]) and _cell_has_formula(cell):
                    cols_to_object.add(col_idx)
                    pending.append((row_idx, col_idx, cell.value))
    finally:
        wb.close()

    if not pending:
        return out

    out = out.astype({out.columns[col_idx]: object for col_idx in cols_to_object})
    for row_idx, col_idx, value in pending:
        out.iloc[row_idx, col_idx] = value
    return out


def load_xlsx_table(path: Path, sheet: str | None = None) -> pd.DataFrame:
    """Load xlsx with cached values where present, formula text otherwise."""
    sheet_arg: str | int = sheet if sheet is not None else 0
    df = pd.read_excel(path, sheet_name=sheet_arg)
    if not df.isna().any().any():
        return df
    resolved = _resolve_sheet_name(path, sheet_arg)
    return _fill_formula_text(df, path, resolved)


def _format_markdown_cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, bool):
        return str(value)
    return str(value)


def dataframe_to_markdown(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in df.itertuples(index=False, name=None):
        cells = [_format_markdown_cell(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def xlsx_to_markdown(path: Path) -> str:
    with pd.ExcelFile(path) as xf:
        sheet_names = xf.sheet_names
    sections: list[str] = []
    for name in sheet_names:
        df = load_xlsx_table(path, sheet=name)
        sections.append(f"## {name}\n{dataframe_to_markdown(df)}")
    return "\n".join(sections)
