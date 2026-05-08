from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from office_mcp.analytics import load_table
from office_mcp.errors import ErrorCategory, OfficeReaderError


@pytest.fixture
def sample_xlsx(tmp_path: Path) -> Path:
    path = tmp_path / "data.xlsx"
    pd.DataFrame({
        "id": [1, 2, 3],
        "status": ["open", "closed", "open"],
        "hours": [4.5, 8.0, 2.0],
    }).to_excel(path, index=False, sheet_name="Sales")
    return path


def test_load_table_default_first_sheet(sample_xlsx):
    df = load_table(sample_xlsx)
    assert list(df.columns) == ["id", "status", "hours"]
    assert len(df) == 3


def test_load_table_named_sheet(sample_xlsx):
    df = load_table(sample_xlsx, sheet="Sales")
    assert len(df) == 3


def test_load_table_unknown_sheet_raises(sample_xlsx):
    with pytest.raises(OfficeReaderError) as ei:
        load_table(sample_xlsx, sheet="Missing")
    assert ei.value.category == ErrorCategory.PARSE_FAILED


def test_load_table_unsupported_extension_raises(tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("not xlsx")
    with pytest.raises(OfficeReaderError) as ei:
        load_table(bad)
    assert ei.value.category == ErrorCategory.UNSUPPORTED_FORMAT


def test_load_table_missing_file_raises(tmp_path):
    with pytest.raises(OfficeReaderError) as ei:
        load_table(tmp_path / "nope.xlsx")
    assert ei.value.category == ErrorCategory.FILE_NOT_FOUND


from office_mcp.analytics import apply_filter  # noqa: E402
from office_mcp.analytics_schema import parse_filter  # noqa: E402


def test_apply_filter_eq(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"col": "status", "op": "==", "value": "open"}
    ]))
    assert len(out) == 2
    assert set(out["status"]) == {"open"}


def test_apply_filter_gt(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"col": "hours", "op": ">", "value": 3.0}
    ]))
    assert len(out) == 2


def test_apply_filter_in(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"col": "id", "op": "in", "value": [1, 3]}
    ]))
    assert sorted(out["id"]) == [1, 3]


def test_apply_filter_contains(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"col": "status", "op": "contains", "value": "ope"}
    ]))
    assert len(out) == 2


def test_apply_filter_between(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"col": "hours", "op": "between", "value": [3, 5]}
    ]))
    assert len(out) == 1


def test_apply_filter_is_null(tmp_path):
    p = tmp_path / "withnulls.xlsx"
    pd.DataFrame({"a": [1, None, 3]}).to_excel(p, index=False)
    df = load_table(p)
    out = apply_filter(df, parse_filter([
        {"col": "a", "op": "is_null"}
    ]))
    assert len(out) == 1


def test_apply_filter_and_combines(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"col": "status", "op": "==", "value": "open"},
        {"col": "hours", "op": ">", "value": 3.0},
    ]))
    assert len(out) == 1
    assert out.iloc[0]["id"] == 1


def test_apply_filter_or_combines(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([
        {"or": [
            {"col": "id", "op": "==", "value": 1},
            {"col": "id", "op": "==", "value": 3},
        ]}
    ]))
    assert sorted(out["id"]) == [1, 3]


def test_apply_filter_unknown_column_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_filter(df, parse_filter([
            {"col": "missing", "op": "==", "value": 1}
        ]))
    assert ei.value.category == ErrorCategory.COLUMN_NOT_FOUND


def test_apply_filter_empty_returns_full_df(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_filter(df, parse_filter([]))
    assert len(out) == len(df)


from office_mcp.analytics import apply_limit, apply_select, apply_sort  # noqa: E402
from office_mcp.analytics_schema import SortSpec  # noqa: E402


def test_apply_select_subset(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_select(df, ["id", "hours"])
    assert list(out.columns) == ["id", "hours"]


def test_apply_select_unknown_column_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_select(df, ["id", "missing"])
    assert ei.value.category == ErrorCategory.COLUMN_NOT_FOUND


def test_apply_select_none_passes_through(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_select(df, None)
    assert list(out.columns) == list(df.columns)


def test_apply_sort_asc(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_sort(df, [SortSpec(col="hours", order="asc")])
    assert list(out["hours"]) == [2.0, 4.5, 8.0]


def test_apply_sort_desc(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_sort(df, [SortSpec(col="hours", order="desc")])
    assert list(out["hours"]) == [8.0, 4.5, 2.0]


def test_apply_sort_multi_column(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_sort(df, [
        SortSpec(col="status", order="asc"),
        SortSpec(col="hours", order="desc"),
    ])
    assert list(out["hours"]) == [8.0, 4.5, 2.0]
    assert list(out["status"]) == ["closed", "open", "open"]


def test_apply_sort_unknown_column_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_sort(df, [SortSpec(col="missing")])
    assert ei.value.category == ErrorCategory.COLUMN_NOT_FOUND


def test_apply_sort_none_passes_through(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_sort(df, None)
    assert len(out) == len(df)


def test_apply_limit(sample_xlsx):
    df = load_table(sample_xlsx)
    assert len(apply_limit(df, 2)) == 2


def test_apply_limit_none_passes_through(sample_xlsx):
    df = load_table(sample_xlsx)
    assert len(apply_limit(df, None)) == 3


from office_mcp.analytics import apply_groupby_agg  # noqa: E402


def test_groupby_single_col_single_agg(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_groupby_agg(df, groupby=["status"], agg={"hours": "sum"})
    by_status = dict(zip(out["status"], out["hours_sum"], strict=True))
    assert by_status["open"] == 6.5
    assert by_status["closed"] == 8.0


def test_groupby_count_id(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_groupby_agg(df, groupby=["status"], agg={"id": "count"})
    by_status = dict(zip(out["status"], out["id_count"], strict=True))
    assert by_status["open"] == 2
    assert by_status["closed"] == 1


def test_groupby_multi_agg(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_groupby_agg(
        df, groupby=["status"], agg={"hours": ["sum", "mean"]}
    )
    assert "hours_sum" in out.columns
    assert "hours_mean" in out.columns


def test_groupby_empty_groupby_returns_totals(sample_xlsx):
    df = load_table(sample_xlsx)
    out = apply_groupby_agg(df, groupby=[], agg={"hours": "sum"})
    assert len(out) == 1
    assert out.iloc[0]["hours_sum"] == 14.5


def test_groupby_unknown_groupby_column_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_groupby_agg(df, groupby=["missing"], agg={"hours": "sum"})
    assert ei.value.category == ErrorCategory.COLUMN_NOT_FOUND


def test_groupby_unknown_agg_column_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_groupby_agg(df, groupby=["status"], agg={"missing": "sum"})
    assert ei.value.category == ErrorCategory.COLUMN_NOT_FOUND


def test_groupby_unsupported_agg_func_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_groupby_agg(df, groupby=["status"], agg={"hours": "rank"})
    assert ei.value.category == ErrorCategory.UNSUPPORTED_AGGREGATION


def test_groupby_sum_on_text_column_raises(sample_xlsx):
    df = load_table(sample_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_groupby_agg(df, groupby=["status"], agg={"status": "sum"})
    assert ei.value.category == ErrorCategory.INCOMPATIBLE_DTYPE


from office_mcp.analytics import apply_pivot  # noqa: E402


@pytest.fixture
def pivot_xlsx(tmp_path: Path) -> Path:
    path = tmp_path / "pivot.xlsx"
    pd.DataFrame({
        "team": ["A", "A", "B", "B", "B"],
        "status": ["open", "closed", "open", "open", "closed"],
        "id": [1, 2, 3, 4, 5],
    }).to_excel(path, index=False)
    return path


def test_pivot_basic(pivot_xlsx):
    df = load_table(pivot_xlsx)
    out = apply_pivot(
        df,
        index="team",
        columns="status",
        values="id",
        aggfunc="count",
        fill_value=0,
    )
    assert "team" in out.columns
    a_row = out[out["team"] == "A"].iloc[0]
    b_row = out[out["team"] == "B"].iloc[0]
    assert a_row["id_open"] == 1
    assert a_row["id_closed"] == 1
    assert b_row["id_open"] == 2
    assert b_row["id_closed"] == 1


def test_pivot_default_aggfunc_is_sum(pivot_xlsx):
    df = load_table(pivot_xlsx)
    out = apply_pivot(df, index="team", columns="status", values="id", fill_value=0)
    a_open = out[out["team"] == "A"].iloc[0]["id_open"]
    assert a_open == 1


def test_pivot_unknown_index_raises(pivot_xlsx):
    df = load_table(pivot_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_pivot(df, index="missing", columns="status", values="id")
    assert ei.value.category == ErrorCategory.COLUMN_NOT_FOUND


def test_pivot_unknown_aggfunc_raises(pivot_xlsx):
    df = load_table(pivot_xlsx)
    with pytest.raises(OfficeReaderError) as ei:
        apply_pivot(df, index="team", columns="status", values="id", aggfunc="rank")
    assert ei.value.category == ErrorCategory.UNSUPPORTED_AGGREGATION


from office_mcp.analytics import summarize_dataframe  # noqa: E402


def test_summarize_text_column(sample_xlsx):
    df = load_table(sample_xlsx)
    summary = summarize_dataframe(df)
    cols = {c["name"]: c for c in summary["columns"]}
    s = cols["status"]
    assert s["dtype"] == "text"
    assert s["n_null"] == 0
    assert s["n_unique"] == 2
    assert set(s["sample"]).issubset({"open", "closed"})


def test_summarize_numeric_column(sample_xlsx):
    df = load_table(sample_xlsx)
    summary = summarize_dataframe(df)
    cols = {c["name"]: c for c in summary["columns"]}
    h = cols["hours"]
    assert h["dtype"] == "number"
    assert h["min"] == 2.0
    assert h["max"] == 8.0
    assert "mean" in h


def test_summarize_n_rows_and_head(sample_xlsx):
    df = load_table(sample_xlsx)
    summary = summarize_dataframe(df)
    assert summary["n_rows"] == 3
    assert len(summary["head"]) == 3   # capped at 3


def test_summarize_head_capped_at_3():
    df = pd.DataFrame({"a": list(range(10))})
    summary = summarize_dataframe(df)
    assert len(summary["head"]) == 3


from io import BytesIO  # noqa: E402

from openpyxl import load_workbook  # noqa: E402

from office_mcp.analytics import dataframe_to_xlsx_bytes  # noqa: E402


def test_dataframe_to_xlsx_bytes_round_trip(sample_xlsx):
    df = load_table(sample_xlsx)
    data = dataframe_to_xlsx_bytes(df, sheet_name="Result")
    wb = load_workbook(BytesIO(data))
    ws = wb["Result"]
    assert [c.value for c in ws[1]] == ["id", "status", "hours"]
    assert ws.cell(2, 1).value == 1
    assert ws.cell(2, 2).value == "open"
    assert ws.cell(2, 3).value == 4.5


def test_dataframe_to_xlsx_bytes_pivot_column_names(pivot_xlsx):
    df = load_table(pivot_xlsx)
    out = apply_pivot(
        df, index="team", columns="status",
        values="id", aggfunc="count", fill_value=0,
    )
    data = dataframe_to_xlsx_bytes(out, sheet_name="Pivot")
    wb = load_workbook(BytesIO(data))
    headers = [c.value for c in wb["Pivot"][1]]
    assert "team" in headers
    assert "id_open" in headers
    assert "id_closed" in headers
