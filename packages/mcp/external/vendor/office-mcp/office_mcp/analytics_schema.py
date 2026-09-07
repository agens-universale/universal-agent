from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FilterOp = Literal[
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in",
    "contains", "startswith", "endswith",
    "is_null", "not_null",
    "between",
]

FilterScalar = str | int | float | bool
FilterValue = FilterScalar | list[FilterScalar] | None


class FilterClause(BaseModel):
    """A single leaf filter clause. OR-grouping is expressed at the parse_filter level."""
    model_config = ConfigDict(extra="forbid")

    col: str
    op: FilterOp
    value: FilterValue = None

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


class OrFilterGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    or_: list[FilterClause | OrFilterGroup] = Field(alias="or")


OrFilterGroup.model_rebuild()

FilterNode = FilterClause | OrFilterGroup


def _coerce_filter_node(node: FilterNode | dict) -> FilterNode:
    if isinstance(node, (FilterClause, OrFilterGroup)):
        return node
    if isinstance(node, dict):
        if "or" in node:
            return OrFilterGroup.model_validate(node)
        return FilterClause.model_validate(node)
    raise TypeError(f"filter node must be dict or FilterNode, got {type(node).__name__}")


def filter_node_to_dict(node: FilterNode | dict) -> dict:
    coerced = _coerce_filter_node(node)
    if isinstance(coerced, OrFilterGroup):
        return {"or": [filter_node_to_dict(child) for child in coerced.or_]}
    return coerced.model_dump()


def filter_nodes_to_raw(nodes: list[FilterNode | dict] | None) -> list[dict]:
    if not nodes:
        return []
    return [filter_node_to_dict(node) for node in nodes]


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
    model_config = ConfigDict(extra="forbid")

    col: str
    order: Literal["asc", "desc"] = "asc"


def sort_specs_to_models(sort: list[SortSpec | dict] | None) -> list[SortSpec]:
    if not sort:
        return []
    out: list[SortSpec] = []
    for item in sort:
        if isinstance(item, SortSpec):
            out.append(item)
        elif isinstance(item, dict):
            out.append(SortSpec.model_validate(item))
        else:
            raise TypeError(f"sort item must be dict or SortSpec, got {type(item).__name__}")
    return out


class AggColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    col: str
    func: str | list[str]


def agg_columns_to_dict(
    columns: list[AggColumnSpec | dict] | None,
) -> dict[str, str | list[str]]:
    if not columns:
        return {}
    out: dict[str, str | list[str]] = {}
    for column in columns:
        spec = (
            column
            if isinstance(column, AggColumnSpec)
            else AggColumnSpec.model_validate(column)
        )
        out[spec.col] = spec.func
    return out


class OutputFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sheet: str = "Sheet1"
    overwrite: bool = False


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
        if isinstance(raw, OutputFileInput):
            return cls(
                kind="file",
                path=raw.path,
                sheet=raw.sheet,
                overwrite=raw.overwrite,
            )
        raise ValueError(f"output must be None | 'data' | dict, got {type(raw).__name__}")
