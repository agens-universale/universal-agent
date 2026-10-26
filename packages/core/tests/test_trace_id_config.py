import pytest
from gc_core.config_modules.root import AppConfig
from pydantic import ValidationError


def _app(raw: dict) -> AppConfig:
    return AppConfig.model_validate(raw)


class TestTraceIdHeaderName:
    def test_default_is_none(self):
        assert _app({}).agent.trace_id_header_name is None

    def test_explicit_value_reaches_settings(self):
        cfg = _app({"agent": {"trace_id_header_name": "X-Trace-Id"}})
        assert cfg.agent.trace_id_header_name == "X-Trace-Id"

    def test_blank_becomes_none(self):
        cfg = _app({"agent": {"trace_id_header_name": "   "}})
        assert cfg.agent.trace_id_header_name is None

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
        raw = {
            "mcp": {
                "servers": {
                    "srv": {"transport": "http", "url": "http://x/mcp"}
                }
            }
        }
        cfg = _app(raw)
        assert cfg.mcp.servers["srv"].trace_header_enabled is False

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
        cfg = _app(raw)
        assert cfg.mcp.servers["srv"].trace_header_enabled is True
