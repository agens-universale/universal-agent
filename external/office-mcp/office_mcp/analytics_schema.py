from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

FilterOp = Literal[
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in",
    "contains", "startswith", "endswith",
    "is_null", "not_null",
    "between",
]


class FilterClause(BaseModel):
    """A single leaf filter clause. OR-grouping is expressed at the parse_filter level."""
    col: str
    op: FilterOp
    value: Any = None

    @model_validator(mode="after")
    def _validate_value_shape(self) -> FilterClause:
        if self.op in ("in", "not_in"):
            if not isinstance(self.value, list):
                raise ValueError(f"{self.op!r} requires a list value")
        elif self.op == "between":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError("'between' requires a two-element list value")
        elif self.op in ("is_null", "not_null"):
            # value is ignored for these
            pass
        elif self.op in ("contains", "startswith", "endswith"):
            if not isinstance(self.value, str):
                raise ValueError(f"{self.op!r} requires a string value")
        return self


def parse_filter(raw: list[dict] | None) -> dict:
    """Parse a top-level filter list into a normalized AND-tree.

    Top-level array == AND of clauses.
    Each item is either a leaf {col, op, value} or an OR-group {"or": [...]}.
    Returns a dict tree: {"kind": "and"|"or", "children": [...]}
    where leaves are validated FilterClause instances.
    """
    if raw is None:
        raw = []
    return {"kind": "and", "children": [_parse_node(item) for item in raw]}


def _parse_node(item: dict) -> dict:
    if isinstance(item, dict) and "or" in item:
        if not isinstance(item["or"], list):
            raise ValueError("'or' must be a list of clauses")
        return {
            "kind": "or",
            "children": [_parse_node(child) for child in item["or"]],
        }
    return {"kind": "leaf", "clause": FilterClause.model_validate(item)}


AGG_FUNCS = frozenset({
    "sum", "mean", "count", "min", "max",
    "median", "std", "first", "last", "nunique",
})


class SortSpec(BaseModel):
    col: str
    order: Literal["asc", "desc"] = "asc"


class OutputSpec(BaseModel):
    kind: Literal["data", "file"]
    path: str | None = None
    sheet: str = "Sheet1"
    overwrite: bool = False

    @classmethod
    def parse(cls, raw: Any) -> OutputSpec:
        """Accept None | "data" | {path, sheet?, overwrite?}."""
        if raw is None or raw == "data":
            return cls(kind="data")
        if isinstance(raw, str):
            raise ValueError(f"output string must be 'data', got {raw!r}")
        if isinstance(raw, dict):
            if "path" not in raw:
                raise ValueError("output dict must contain 'path'")
            return cls(
                kind="file",
                path=raw["path"],
                sheet=raw.get("sheet", "Sheet1"),
                overwrite=raw.get("overwrite", False),
            )
        raise ValueError(f"output must be None | 'data' | dict, got {type(raw).__name__}")
