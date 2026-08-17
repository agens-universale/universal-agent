from unittest.mock import MagicMock

import pytest

from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars


def _make_mock_resp(lines):
    resp = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    return resp


def test_mcp_debug_off_does_not_print(monkeypatch, capsys):
    monkeypatch.setattr(env_vars, "env", EnvVars(mcp_debug=False))

    from gc_core.tools.connect_mcp.transport import _read_sse_first_json

    resp = _make_mock_resp([
        "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}",
        "",
    ])
    result = _read_sse_first_json(resp, timeout=1.0)
    assert result.get("result") == {"ok": True}
    captured = capsys.readouterr()
    assert "[MCP_DEBUG SSE]" not in captured.out


def test_mcp_debug_on_prints_lines(monkeypatch, capsys):
    monkeypatch.setattr(env_vars, "env", EnvVars(mcp_debug=True))

    from gc_core.tools.connect_mcp.transport import _read_sse_first_json

    resp = _make_mock_resp([
        "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}",
        "",
    ])
    _read_sse_first_json(resp, timeout=1.0)
    captured = capsys.readouterr()
    assert "[MCP_DEBUG SSE]" in captured.out
