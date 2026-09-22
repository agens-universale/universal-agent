from unittest.mock import MagicMock

from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars


def _make_mock_resp(lines):
    async def _aiter():
        for line in lines:
            yield line

    resp = MagicMock()
    # Корутинная форма читает `aiter_lines`; синхронный слой транспорта снят
    # вместе с маршрутом, который его звал.
    resp.aiter_lines = _aiter
    resp.iter_lines.return_value = iter(lines)
    return resp


async def test_mcp_debug_off_does_not_print(monkeypatch, capsys):
    monkeypatch.setattr(env_vars, "env", EnvVars(mcp_debug=False))

    from gc_core.tools.connect_mcp.transport import _aread_sse_first_json

    resp = _make_mock_resp([
        "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}",
        "",
    ])
    result = await _aread_sse_first_json(resp)
    assert result.get("result") == {"ok": True}
    captured = capsys.readouterr()
    assert "[MCP_DEBUG SSE]" not in captured.out


async def test_mcp_debug_on_prints_lines(monkeypatch, capsys):
    monkeypatch.setattr(env_vars, "env", EnvVars(mcp_debug=True))

    from gc_core.tools.connect_mcp.transport import _aread_sse_first_json

    resp = _make_mock_resp([
        "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}",
        "",
    ])
    await _aread_sse_first_json(resp)
    captured = capsys.readouterr()
    assert "[MCP_DEBUG SSE]" in captured.out
