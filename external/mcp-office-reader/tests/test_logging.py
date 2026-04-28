import json
import logging

from mcp_office_reader.logging_config import configure_logging, mask_base64_in_args


def test_mask_base64_in_args_replaces_long_content():
    args = {"filename": "x.doc", "content_base64": "A" * 5000}
    masked = mask_base64_in_args(args)
    assert masked["filename"] == "x.doc"
    assert masked["content_base64"] == "<base64: 5000 bytes>"


def test_mask_base64_keeps_other_fields_intact():
    args = {"path": "/x/y.docx"}
    assert mask_base64_in_args(args) == {"path": "/x/y.docx"}


def test_configure_logging_produces_json(capsys):
    configure_logging("INFO")
    log = logging.getLogger("mcp_office_reader.test")
    log.info("hello", extra={"event": "tool_call", "tool": "read_file"})
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["message"] == "hello"
    assert record["event"] == "tool_call"
    assert record["tool"] == "read_file"
