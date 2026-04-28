from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from office_mcp.server import ServerConfig, build_server


@pytest.fixture
def cfg(tmp_path):
    return ServerConfig(
        allowed_dirs=[str(tmp_path)],
        allow_any=False,
        max_file_size=50 * 1024 * 1024,
        max_output_chars=1_000_000,
        timeout_s=60,
        max_output_base64_size=20 * 1024 * 1024,
        output_allowed_dirs=[str(tmp_path)],
        max_query_rows=1000,
    )


@pytest.fixture
def sample_path(tmp_path: Path) -> Path:
    p = tmp_path / "data.xlsx"
    pd.DataFrame({
        "id": [1, 2, 3],
        "status": ["open", "closed", "open"],
        "hours": [4.5, 8.0, 2.0],
    }).to_excel(p, index=False, sheet_name="Sales")
    return p


def _find_tool(mcp, name):
    return mcp._tool_manager._tools[name].fn


def test_inspect_xlsx_returns_schema_for_default_sheet(cfg, sample_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "inspect_xlsx")
    res = fn(str(sample_path), None)
    assert "sheets" in res
    assert len(res["sheets"]) == 1
    sheet = res["sheets"][0]
    assert sheet["n_rows"] == 3
    col_names = [c["name"] for c in sheet["columns"]]
    assert col_names == ["id", "status", "hours"]


def test_inspect_xlsx_named_sheet(cfg, sample_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "inspect_xlsx")
    res = fn(str(sample_path), "Sales")
    assert res["sheets"][0]["n_rows"] == 3


from openpyxl import load_workbook  # noqa: E402

from office_mcp.errors import ErrorCategory, OfficeReaderError  # noqa: E402


def test_query_xlsx_returns_filtered_rows_inline(cfg, sample_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "query_xlsx")
    res = fn(
        path=str(sample_path),
        sheet=None,
        filter=[{"col": "status", "op": "==", "value": "open"}],
        select=None,
        sort=None,
        limit=None,
        output=None,
    )
    assert res["n_returned"] == 2
    assert res["n_total_after_filter"] == 2
    assert all(r["status"] == "open" for r in res["rows"])


def test_query_xlsx_writes_to_file_when_output_path(cfg, sample_path, tmp_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "query_xlsx")
    out_path = tmp_path / "filtered.xlsx"
    res = fn(
        path=str(sample_path),
        sheet=None,
        filter=[{"col": "status", "op": "==", "value": "open"}],
        select=["id", "hours"],
        sort=None,
        limit=None,
        output={"path": str(out_path), "sheet": "Open"},
    )
    assert res["n_rows_written"] == 2
    assert out_path.exists()
    wb = load_workbook(out_path)
    ws = wb["Open"]
    assert [c.value for c in ws[1]] == ["id", "hours"]


def test_query_xlsx_too_many_rows_raises_when_inline(cfg, sample_path):
    cfg.max_query_rows = 1
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "query_xlsx")
    with pytest.raises(OfficeReaderError) as ei:
        fn(
            path=str(sample_path),
            sheet=None,
            filter=None,
            select=None,
            sort=None,
            limit=None,
            output="data",
        )
    assert ei.value.category == ErrorCategory.OUTPUT_TOO_LARGE


def test_aggregate_xlsx_groupby_sum(cfg, sample_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "aggregate_xlsx")
    res = fn(
        path=str(sample_path),
        sheet=None,
        filter=None,
        groupby=["status"],
        agg={"hours": "sum"},
        having=None,
        sort=None,
        limit=None,
        output=None,
    )
    by_status = {r["status"]: r["hours_sum"] for r in res["rows"]}
    assert by_status["open"] == 6.5
    assert by_status["closed"] == 8.0


def test_aggregate_xlsx_totals_when_no_groupby(cfg, sample_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "aggregate_xlsx")
    res = fn(
        path=str(sample_path),
        sheet=None,
        filter=None,
        groupby=[],
        agg={"hours": "sum", "id": "count"},
        having=None,
        sort=None,
        limit=None,
        output=None,
    )
    assert len(res["rows"]) == 1
    assert res["rows"][0]["hours_sum"] == 14.5
    assert res["rows"][0]["id_count"] == 3


def test_aggregate_xlsx_having_filters_after(cfg, sample_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "aggregate_xlsx")
    res = fn(
        path=str(sample_path),
        sheet=None,
        filter=None,
        groupby=["status"],
        agg={"hours": "sum"},
        having=[{"col": "hours_sum", "op": ">", "value": 7}],
        sort=None,
        limit=None,
        output=None,
    )
    assert len(res["rows"]) == 1
    assert res["rows"][0]["status"] == "closed"


def test_pivot_xlsx_basic(cfg, tmp_path):
    p = tmp_path / "pivot.xlsx"
    pd.DataFrame({
        "team": ["A", "A", "B", "B", "B"],
        "status": ["open", "closed", "open", "open", "closed"],
        "id": [1, 2, 3, 4, 5],
    }).to_excel(p, index=False)
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "pivot_xlsx")
    res = fn(
        path=str(p),
        sheet=None,
        filter=None,
        index="team",
        columns="status",
        values="id",
        aggfunc="count",
        fill_value=0,
        sort=None,
        limit=None,
        output=None,
    )
    by_team = {r["team"]: r for r in res["rows"]}
    assert by_team["A"]["id_open"] == 1
    assert by_team["B"]["id_open"] == 2


def test_pivot_xlsx_writes_to_file(cfg, tmp_path):
    p = tmp_path / "pivot.xlsx"
    pd.DataFrame({
        "team": ["A", "B"],
        "status": ["open", "closed"],
        "id": [1, 2],
    }).to_excel(p, index=False)
    out_path = tmp_path / "pivot_out.xlsx"
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "pivot_xlsx")
    res = fn(
        path=str(p),
        sheet=None,
        filter=None,
        index="team",
        columns="status",
        values="id",
        aggfunc="sum",
        fill_value=0,
        sort=None,
        limit=None,
        output={"path": str(out_path)},
    )
    assert out_path.exists()
    assert res["n_rows_written"] == 2
