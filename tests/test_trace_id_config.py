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

    def test_rejects_del_character(self):
        with pytest.raises(ValidationError):
            _app({"agent": {"trace_id_header_name": "X-Trace\x7fId"}})


class TestMcpTraceHeaderFlag:
    def test_default_false(self):
        """Рассылка trace — opt-in: без явного разрешения сервер его не получает."""
        raw = {"mcp": {"servers": {"srv": {"transport": "http", "url": "http://x/mcp"}}}}
        cfg = flatten_app_config(_app(raw))
        assert cfg["mcp-servers"]["srv"]["trace_header_enabled"] is False

    def test_can_be_enabled(self):
        raw = {
            "mcp": {
                "servers": {
                    "srv": {
                        "transport": "http",
                        "url": "http://x/mcp",
                        "trace_header_enabled": True,
                    }
                }
            }
        }
        cfg = flatten_app_config(_app(raw))
        assert cfg["mcp-servers"]["srv"]["trace_header_enabled"] is True
