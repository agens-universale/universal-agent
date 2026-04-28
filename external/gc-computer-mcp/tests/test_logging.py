import json
import logging

from gc_computer_mcp.logging_config import configure_logging, mask_type_args


def test_json_format_to_stderr(capsys):
    configure_logging(level="INFO")
    logger = logging.getLogger("gc_computer_mcp.test")
    logger.info("hello", extra={"event": "tool_call", "tool": "click"})
    captured = capsys.readouterr()
    assert captured.out == ""
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["event"] == "tool_call"
    assert record["tool"] == "click"
    assert record["level"] == "INFO"
    assert "ts" in record


def test_mask_type_args_replaces_text_with_length():
    args = {"element_id": 3, "text": "secret_password"}
    masked = mask_type_args(args)
    assert masked["element_id"] == 3
    assert masked["text_length"] == len("secret_password")
    assert "text" not in masked


def test_mask_type_args_preserves_non_text_args():
    args = {"element_id": 3}
    masked = mask_type_args(args)
    assert masked == args
