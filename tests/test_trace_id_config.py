import pytest
from pydantic import ValidationError

from config import flatten_app_config
from config_modules.root import AppConfig


def _app(raw: dict) -> AppConfig:
    return AppConfig.model_validate(raw)


class TestTraceIdHeaderName:
    def test_default_is_none(self):
        cfg = flatten_app_config(_app({}))
        assert cfg["trace_id_header_name"] is None

    def test_explicit_value_reaches_flat_config(self):
        cfg = flatten_app_config(_app({"agent": {"trace_id_header_name": "X-Trace-Id"}}))
        assert cfg["trace_id_header_name"] == "X-Trace-Id"

    def test_blank_becomes_none(self):
        cfg = flatten_app_config(_app({"agent": {"trace_id_header_name": "   "}}))
        assert cfg["trace_id_header_name"] is None

    def test_rejects_spaces_and_colon(self):
        for bad in ("X Trace", "X-Trace:", "X\nTrace"):
            with pytest.raises(ValidationError):
                _app({"agent": {"trace_id_header_name": bad}})


class TestMcpTraceHeaderFlag:
    def test_default_true(self):
        raw = {"mcp": {"servers": {"srv": {"transport": "http", "url": "http://x/mcp"}}}}
        cfg = flatten_app_config(_app(raw))
        assert cfg["mcp-servers"]["srv"]["trace_header"] is True

    def test_can_be_disabled(self):
        raw = {
            "mcp": {
                "servers": {
                    "srv": {
                        "transport": "http",
                        "url": "http://x/mcp",
                        "trace_header": False,
                    }
                }
            }
        }
        cfg = flatten_app_config(_app(raw))
        assert cfg["mcp-servers"]["srv"]["trace_header"] is False
