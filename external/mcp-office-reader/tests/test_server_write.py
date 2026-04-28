from __future__ import annotations

import base64
from io import BytesIO

import pytest
from docx import Document
from openpyxl import load_workbook

from mcp_office_reader.errors import ErrorCategory, OfficeReaderError
from mcp_office_reader.server import ServerConfig, build_server


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
    )


def _find_tool(mcp, name):
    # FastMCP stores tools in mcp._tool_manager._tools (private, but stable enough).
    return mcp._tool_manager._tools[name].fn


def test_create_docx_file_writes_docx(cfg, tmp_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_docx_file")
    out = tmp_path / "hello.docx"
    res = fn(str(out), "# Hello\n\nWorld.", False)
    assert res["path"].endswith("hello.docx")
    assert out.exists()
    d = Document(str(out))
    assert any(p.text == "Hello" for p in d.paragraphs)


def test_create_docx_returns_base64(cfg):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_docx")
    res = fn("hello.docx", "# Hello\n")
    data = base64.b64decode(res["content_base64"])
    d = Document(BytesIO(data))
    assert any(p.text == "Hello" for p in d.paragraphs)


def test_create_docx_file_refuses_existing(cfg, tmp_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_docx_file")
    out = tmp_path / "hello.docx"
    out.write_bytes(b"existing")
    with pytest.raises(OfficeReaderError) as ei:
        fn(str(out), "# Hi", False)
    assert ei.value.category == ErrorCategory.FILE_EXISTS


def test_create_xlsx_file_writes_xlsx(cfg, tmp_path):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_xlsx_file")
    out = tmp_path / "data.xlsx"
    workbook = {
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "integer"}],
            "rows": [[1], [2]],
        }]
    }
    res = fn(str(out), workbook, False)
    assert out.exists()
    wb = load_workbook(str(out))
    assert wb["S"].cell(2, 1).value == 1
    assert wb["S"].cell(3, 1).value == 2
    assert res["bytes_written"] > 0


def test_create_xlsx_returns_base64(cfg):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_xlsx")
    workbook = {
        "sheets": [{
            "name": "S",
            "columns": [{"name": "A", "type": "integer"}],
            "rows": [[42]],
        }]
    }
    res = fn("data.xlsx", workbook)
    data = base64.b64decode(res["content_base64"])
    wb = load_workbook(BytesIO(data))
    assert wb["S"].cell(2, 1).value == 42


def test_create_xlsx_invalid_input_surfaces_error(cfg):
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_xlsx")
    with pytest.raises(OfficeReaderError) as ei:
        fn("x.xlsx", {"sheets": []})  # invalid: empty sheets
    assert ei.value.category == ErrorCategory.INVALID_INPUT


def test_write_respects_output_allowed_dirs_distinct_from_read(tmp_path):
    writes = tmp_path / "writes"
    writes.mkdir()
    cfg = ServerConfig(
        allowed_dirs=[str(tmp_path)],
        allow_any=False,
        max_file_size=50 * 1024 * 1024,
        max_output_chars=1_000_000,
        timeout_s=60,
        max_output_base64_size=20 * 1024 * 1024,
        output_allowed_dirs=[str(writes)],
    )
    mcp = build_server(cfg)
    fn = _find_tool(mcp, "create_docx_file")
    # writing into the read-only dir should be refused
    with pytest.raises(OfficeReaderError) as ei:
        fn(str(tmp_path / "blocked.docx"), "# x", False)
    assert ei.value.category == ErrorCategory.PATH_NOT_ALLOWED
    # writing inside the write-only dir should succeed
    out = fn(str(writes / "ok.docx"), "# x", False)
    assert (writes / "ok.docx").exists()
    assert out["bytes_written"] > 0
