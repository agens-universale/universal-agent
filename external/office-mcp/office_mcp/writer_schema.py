from __future__ import annotations

import datetime as _dt
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .writer_a1 import parse_a1_range

_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_FORBIDDEN_SHEET_CHARS = set(":\\/?*[]")

ColumnType = Literal[
    "text", "number", "integer", "date", "datetime",
    "bool", "currency", "percent",
    "formula",
]


class ColumnSpec(BaseModel):
    name: str
    type: ColumnType
    format: str | None = None
    width: float | Literal["auto"] | None = "auto"

    @field_validator("name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("column name must be non-empty")
        return v

    @field_validator("format")
    @classmethod
    def _format_nonempty(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("format must be non-empty (use null to let defaults apply)")
        return v


class CellStyle(BaseModel):
    range: str
    bg: str | None = None
    fg: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    align: Literal["left", "center", "right"] | None = None
    font_size: float | None = None
    font_name: str | None = None
    border: Literal["all", "none"] | None = None

    @field_validator("bg", "fg")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v is not None and not _HEX_RE.match(v):
            raise ValueError(f"color must be 6-hex, got {v!r}")
        return v.upper() if v else v

    @field_validator("range")
    @classmethod
    def _range_ok(cls, v: str) -> str:
        parse_a1_range(v)  # raises ValueError on bad format
        return v

    @field_validator("font_size")
    @classmethod
    def _font_size_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"font_size must be > 0, got {v}")
        return v

    @field_validator("font_name")
    @classmethod
    def _font_name_nonempty(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("font_name must be non-empty (use null to skip)")
        return v


CellValue = str | int | float | bool | None


class SheetSpec(BaseModel):
    name: str
    columns: list[ColumnSpec]
    rows: list[list[CellValue]]
    freeze_header: bool = True
    auto_filter: bool = False
    merges: list[str] = Field(default_factory=list)
    cell_styles: list[CellStyle] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not v or len(v) > 31:
            raise ValueError(f"sheet name must be 1..31 chars, got {len(v)}")
        if any(c in _FORBIDDEN_SHEET_CHARS for c in v):
            raise ValueError(f"sheet name contains forbidden chars: {v!r}")
        return v

    @field_validator("columns")
    @classmethod
    def _at_least_one_column(cls, v: list[ColumnSpec]) -> list[ColumnSpec]:
        if not v:
            raise ValueError("sheet must have at least one column")
        return v

    @model_validator(mode="after")
    def _validate_rows_and_ranges(self) -> SheetSpec:
        n_cols = len(self.columns)
        max_row = len(self.rows) + 1  # +1 for header
        types = [c.type for c in self.columns]

        for i, row in enumerate(self.rows):
            if len(row) != n_cols:
                raise ValueError(
                    f"row {i} has {len(row)} items, expected {n_cols}"
                )
            for j, val in enumerate(row):
                if val is None:
                    continue
                self._check_cell(i, j, types[j], val)

        for r in self.merges:
            cmin, rmin, cmax, rmax = parse_a1_range(r)
            if cmax > n_cols or rmax > max_row or rmin < 1 or cmin < 1:
                raise ValueError(
                    f"merge {r!r} out of bounds (sheet cols=1..{n_cols}, "
                    f"rows=1..{max_row})"
                )
        for cs in self.cell_styles:
            cmin, rmin, cmax, rmax = parse_a1_range(cs.range)
            if cmax > n_cols or rmax > max_row or rmin < 1 or cmin < 1:
                raise ValueError(
                    f"cell_style range {cs.range!r} out of bounds "
                    f"(cols=1..{n_cols}, rows=1..{max_row})"
                )
        return self

    @staticmethod
    def _check_cell(i: int, j: int, typ: ColumnType, val: CellValue) -> None:
        if typ == "text":
            if not isinstance(val, str):
                raise ValueError(
                    f"row {i} col {j}: expected text, got {type(val).__name__}"
                )
        elif typ in ("number", "currency", "percent"):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"row {i} col {j}: expected number")
        elif typ == "integer":
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(f"row {i} col {j}: expected integer")
        elif typ == "bool":
            if not isinstance(val, bool):
                raise ValueError(f"row {i} col {j}: expected bool")
        elif typ == "date":
            if not isinstance(val, str):
                raise ValueError(f"row {i} col {j}: expected ISO date string")
            try:
                _dt.date.fromisoformat(val)
            except ValueError as e:
                raise ValueError(f"row {i} col {j}: bad ISO date {val!r}: {e}") from e
        elif typ == "datetime":
            if not isinstance(val, str):
                raise ValueError(f"row {i} col {j}: expected ISO datetime string")
            try:
                _dt.datetime.fromisoformat(val)
            except ValueError as e:
                raise ValueError(f"row {i} col {j}: bad ISO datetime {val!r}: {e}") from e
        elif typ == "formula":
            if not isinstance(val, str):
                raise ValueError(f"row {i} col {j}: formula must be a string")
            if not val.startswith("="):
                raise ValueError(
                    f"row {i} col {j}: formula must start with '=', got {val!r}"
                )

    def coerced_rows(self) -> list[list[CellValue | _dt.date | _dt.datetime]]:
        """Return rows with ISO date/datetime strings converted to Python objects.

        Other cell values pass through unchanged. None stays None.
        """
        types = [c.type for c in self.columns]
        out: list[list[CellValue | _dt.date | _dt.datetime]] = []
        for i, row in enumerate(self.rows):
            new_row: list[CellValue | _dt.date | _dt.datetime] = []
            for j, val in enumerate(row):
                if val is None:
                    new_row.append(None)
                elif types[j] == "date" and isinstance(val, str):
                    try:
                        new_row.append(_dt.date.fromisoformat(val))
                    except ValueError as e:
                        raise ValueError(
                            f"row {i} col {j}: bad ISO date {val!r}: {e}"
                        ) from e
                elif types[j] == "datetime" and isinstance(val, str):
                    try:
                        new_row.append(_dt.datetime.fromisoformat(val))
                    except ValueError as e:
                        raise ValueError(
                            f"row {i} col {j}: bad ISO datetime {val!r}: {e}"
                        ) from e
                else:
                    new_row.append(val)
            out.append(new_row)
        return out


class WorkbookSpec(BaseModel):
    sheets: list[SheetSpec]

    @field_validator("sheets")
    @classmethod
    def _at_least_one(cls, v: list[SheetSpec]) -> list[SheetSpec]:
        if not v:
            raise ValueError("workbook must have at least one sheet")
        return v

    @model_validator(mode="after")
    def _unique_names(self) -> WorkbookSpec:
        names = [s.name for s in self.sheets]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate sheet name in {names}")
        return self
