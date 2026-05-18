from __future__ import annotations

from office_mcp.reader import read_document_from_bytes
from office_mcp.server import ServerConfig, build_server


def test_xlsx_roundtrip(tmp_path):
    cfg = ServerConfig(
        allowed_dirs=[str(tmp_path)],
        allow_any=False,
        max_file_size=50 * 1024 * 1024,
        max_output_chars=1_000_000,
        timeout_s=60,
        max_output_base64_size=20 * 1024 * 1024,
        output_allowed_dirs=[str(tmp_path)],
    )
    mcp = build_server(cfg)
    fn = mcp._tool_manager._tools["create_xlsx"].fn
    res = fn(
        "rt.xlsx",
        {
            "sheets": [
                {
                    "name": "S",
                    "columns": [
                        {"name": "Name", "type": "text"},
                        {"name": "Age", "type": "integer"},
                    ],
                    "rows": [["Alice", 30], ["Bob", 25]],
                }
            ]
        },
    )
    md = read_document_from_bytes(
        filename="rt.xlsx",
        content_base64=res["content_base64"],
        max_file_size=cfg.max_file_size,
        max_output_chars=cfg.max_output_chars,
        timeout_s=cfg.timeout_s,
    )
    assert "Alice" in md
    assert "30" in md
    assert "Bob" in md
