from __future__ import annotations

from pathlib import Path

import pandas as pd

from .analytics_schema import AGG_FUNCS
from .errors import ErrorCategory, OfficeReaderError

_NUMERIC_AGGS = {"sum", "mean", "median", "std"}

SUPPORTED_ANALYTIC_EXT = (".xlsx", ".xls", ".csv")


def load_table(path: Path | str, sheet: str | None = None) -> pd.DataFrame:
    """Load a table file into a pandas DataFrame.

    Sheet selection only applies to xlsx/xls. CSV ignores `sheet`.
    Raises OfficeReaderError with a typed category on common failures.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise OfficeReaderError(ErrorCategory.FILE_NOT_FOUND, path=str(p))
    ext = p.suffix.lower()
    if ext not in SUPPORTED_ANALYTIC_EXT:
        raise OfficeReaderError(ErrorCategory.UNSUPPORTED_FORMAT, ext=ext)

    try:
        if ext == ".csv":
            return pd.read_csv(p)
        return pd.read_excel(p, sheet_name=sheet if sheet else 0)
    except OfficeReaderError:
        raise
    except Exception as e:
        raise OfficeReaderError(
            ErrorCategory.PARSE_FAILED, path=str(p), fmt=ext
        ) from e


def apply_filter(df: pd.DataFrame, filter_tree: dict) -> pd.DataFrame:
    """Apply a parsed filter tree (from analytics_schema.parse_filter) to df."""
    mask = _eval_node(df, filter_tree)
    return df[mask]


def _eval_node(df: pd.DataFrame, node: dict) -> pd.Series:
    kind = node["kind"]
    if kind == "and":
        children = node["children"]
        if not children:
            return pd.Series(True, index=df.index)
        m = _eval_node(df, children[0])
        for c in children[1:]:
            m = m & _eval_node(df, c)
        return m
    if kind == "or":
        children = node["children"]
        if not children:
            return pd.Series(False, index=df.index)
        m = _eval_node(df, children[0])
        for c in children[1:]:
            m = m | _eval_node(df, c)
        return m
    if kind == "leaf":
        return _eval_leaf(df, node["clause"])
    raise ValueError(f"unknown filter node kind: {kind}")


def _eval_leaf(df: pd.DataFrame, clause) -> pd.Series:
    if clause.col not in df.columns:
        raise OfficeReaderError(
            ErrorCategory.COLUMN_NOT_FOUND,
            column=clause.col,
            available=list(df.columns),
        )
    series = df[clause.col]
    op, value = clause.op, clause.value
    if op == "==":
        return series == value
    if op == "!=":
        return series != value
    if op == ">":
        return series > value
    if op == ">=":
        return series >= value
    if op == "<":
        return series < value
    if op == "<=":
        return series <= value
    if op == "in":
        return series.isin(value)
    if op == "not_in":
        return ~series.isin(value)
    if op == "contains":
        return series.astype(str).str.contains(value, na=False, regex=False)
    if op == "startswith":
        return series.astype(str).str.startswith(value, na=False)
    if op == "endswith":
        return series.astype(str).str.endswith(value, na=False)
    if op == "is_null":
        return series.isna()
    if op == "not_null":
        return series.notna()
    if op == "between":
        lo, hi = value
        return (series >= lo) & (series <= hi)
    raise ValueError(f"unknown operator: {op}")


def apply_select(df: pd.DataFrame, select: list[str] | None) -> pd.DataFrame:
    if not select:
        return df
    missing = [c for c in select if c not in df.columns]
    if missing:
        raise OfficeReaderError(
            ErrorCategory.COLUMN_NOT_FOUND,
            column=missing[0],
            available=list(df.columns),
        )
    return df[select]


def apply_sort(df: pd.DataFrame, sort: list | None) -> pd.DataFrame:
    if not sort:
        return df
    cols = [s.col for s in sort]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise OfficeReaderError(
            ErrorCategory.COLUMN_NOT_FOUND,
            column=missing[0],
            available=list(df.columns),
        )
    return df.sort_values(by=cols, ascending=[s.order == "asc" for s in sort])


def apply_limit(df: pd.DataFrame, limit: int | None) -> pd.DataFrame:
    if limit is None:
        return df
    return df.head(limit)


def apply_groupby_agg(
    df: pd.DataFrame,
    *,
    groupby: list[str],
    agg: dict[str, str | list[str]],
) -> pd.DataFrame:
    """groupby (possibly empty) + per-column agg(s). Returns a flat DataFrame.

    Result columns are named `<col>_<func>` for both single and multi-agg cases.
    """
    _validate_columns(df, groupby, what="groupby")
    _validate_columns(df, list(agg.keys()), what="agg")
    _validate_agg_funcs(agg)
    _validate_agg_dtypes(df, agg)

    if not groupby:
        out_data = {}
        for col, funcs in _normalize_agg(agg).items():
            for f in funcs:
                out_data[f"{col}_{f}"] = [_apply_single_agg(df[col], f)]
        return pd.DataFrame(out_data)

    grouped = df.groupby(groupby, dropna=False)
    pieces = []
    for col, funcs in _normalize_agg(agg).items():
        for f in funcs:
            series = grouped[col].agg(f)
            series.name = f"{col}_{f}"
            pieces.append(series)
    out = pd.concat(pieces, axis=1).reset_index()
    return out


def _normalize_agg(agg: dict[str, str | list[str]]) -> dict[str, list[str]]:
    return {col: ([fn] if isinstance(fn, str) else list(fn)) for col, fn in agg.items()}


def _apply_single_agg(series: pd.Series, func: str):
    if func == "first":
        return series.iloc[0] if len(series) else None
    if func == "last":
        return series.iloc[-1] if len(series) else None
    return series.agg(func)


def _validate_columns(df: pd.DataFrame, cols: list[str], what: str) -> None:
    for c in cols:
        if c not in df.columns:
            raise OfficeReaderError(
                ErrorCategory.COLUMN_NOT_FOUND,
                column=c,
                available=list(df.columns),
            )


def _validate_agg_funcs(agg: dict[str, str | list[str]]) -> None:
    for funcs in agg.values():
        for f in ([funcs] if isinstance(funcs, str) else funcs):
            if f not in AGG_FUNCS:
                raise OfficeReaderError(
                    ErrorCategory.UNSUPPORTED_AGGREGATION,
                    func=f,
                    supported=sorted(AGG_FUNCS),
                )


def _validate_agg_dtypes(df: pd.DataFrame, agg: dict[str, str | list[str]]) -> None:
    for col, funcs in _normalize_agg(agg).items():
        is_numeric = pd.api.types.is_numeric_dtype(df[col])
        for f in funcs:
            if f in _NUMERIC_AGGS and not is_numeric:
                raise OfficeReaderError(
                    ErrorCategory.INCOMPATIBLE_DTYPE,
                    column=col,
                    dtype="text" if pd.api.types.is_object_dtype(df[col]) else str(df[col].dtype),
                    operation=f,
                )


def apply_pivot(
    df: pd.DataFrame,
    *,
    index: str | list[str],
    columns: str | list[str],
    values: str,
    aggfunc: str = "sum",
    fill_value=None,
) -> pd.DataFrame:
    """pivot_table wrapper. Returns flat DataFrame; pivot columns become `<values>_<col_value>`."""
    index_list = [index] if isinstance(index, str) else list(index)
    columns_list = [columns] if isinstance(columns, str) else list(columns)

    _validate_columns(df, index_list + columns_list + [values], what="pivot")
    if aggfunc not in AGG_FUNCS:
        raise OfficeReaderError(
            ErrorCategory.UNSUPPORTED_AGGREGATION,
            func=aggfunc,
            supported=sorted(AGG_FUNCS),
        )

    pv = pd.pivot_table(
        df,
        index=index_list,
        columns=columns_list,
        values=values,
        aggfunc=aggfunc,
        fill_value=fill_value,
    )
    pv = pv.reset_index()
    pv.columns = [
        _flatten_pivot_column(c, index_set=set(index_list), values_name=values)
        for c in pv.columns
    ]
    return pv


def _flatten_pivot_column(col, *, index_set: set[str], values_name: str) -> str:
    """Flatten a pivot column: index columns keep their name; pivoted ones get values_-prefix."""
    if isinstance(col, tuple):
        parts = [str(p) for p in col if p != ""]
        flat = "_".join(parts)
    else:
        flat = str(col)
    if flat in index_set:
        return flat
    # pivoted column: prefix with values_name unless already prefixed
    if flat.startswith(f"{values_name}_") or flat == values_name:
        return flat
    return f"{values_name}_{flat}"


_HEAD_ROWS = 3
_SAMPLE_VALUES = 5


def summarize_dataframe(df: pd.DataFrame) -> dict:
    """Schema + sample summary for one DataFrame (one sheet)."""
    cols = []
    for name in df.columns:
        s = df[name]
        col_summary = {
            "name": str(name),
            "dtype": _normalize_dtype(s),
            "n_null": int(s.isna().sum()),
            "n_unique": int(s.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            col_summary["min"] = _scalar(s.min())
            col_summary["max"] = _scalar(s.max())
            col_summary["mean"] = _scalar(s.mean())
        else:
            col_summary["sample"] = [
                _scalar(v) for v in s.dropna().unique()[:_SAMPLE_VALUES]
            ]
        cols.append(col_summary)
    return {
        "n_rows": int(len(df)),
        "columns": cols,
        "head": [
            [_scalar(v) for v in row]
            for row in df.head(_HEAD_ROWS).itertuples(index=False, name=None)
        ],
    }


def _normalize_dtype(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "bool"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_float_dtype(s):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    return "text"


def _scalar(v):
    """Convert numpy/pandas scalar to Python primitive for JSON serialization."""
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def dataframe_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    """Convert DataFrame to xlsx bytes using the existing generate_xlsx pipeline."""
    from .writer import generate_xlsx
    from .writer_schema import WorkbookSpec

    columns = []
    for name in df.columns:
        s = df[name]
        columns.append({"name": str(name), "type": _normalize_dtype(s)})
    rows = [
        [_to_jsonable(v) for v in row]
        for row in df.itertuples(index=False, name=None)
    ]
    spec = WorkbookSpec.model_validate({
        "sheets": [{"name": sheet_name, "columns": columns, "rows": rows}],
    })
    return generate_xlsx(spec)


def _to_jsonable(v):
    """Like _scalar, but date/datetime are serialized to ISO strings as our schema expects."""
    if pd.isna(v):
        return None
    import datetime as _dt
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v
