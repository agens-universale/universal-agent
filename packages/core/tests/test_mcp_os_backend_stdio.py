import json

import pytest

from gc_core.tools.connect_mcp.os_backend_client import (
    McpExecCarrierError,
    OsBackendStatelessClient,
)


class _ExecResult:
    def __init__(self, stdout="", stderr="", return_code=0,
                 stdout_hit_capture_cap=False):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.timed_out = False
        self.stdout_hit_capture_cap = stdout_hit_capture_cap
        self.stderr_hit_capture_cap = False


class _StdioSandbox:
    is_sandbox = True

    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def exec_argv(self, argv, *, stdin=None, cwd=None, timeout=None, env=None,
                  capture_max_chars=None):
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": env,
                           "timeout": timeout, "capture_max_chars": capture_max_chars})
        return _ExecResult(stdout="\n".join(self.lines))

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        return _ExecResult(stdout="/usr/bin/python3")


def _frame(rpc_id, result):
    return json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def test_stdio_list_tools_runs_the_command_in_the_backend():
    backend = _StdioSandbox([
        _frame(1, {"protocolVersion": "2024-11-05"}),
        _frame("gc-tools/list", {"tools": [{"name": "echo"}]}),
    ])
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "stdio", "command": "python3",
         "args": ["/srv/mcp_server.py"], "execute_on": "os_backend"},
        os_backend=backend,
    )
    assert client.list_tools(timeout=20.0) == [{"name": "echo"}]
    assert backend.calls[0]["argv"] == ["python3", "/srv/mcp_server.py"]


def test_stdio_sends_the_full_handshake_on_stdin():
    backend = _StdioSandbox([
        _frame(1, {}),
        _frame("gc-tools/list", {"tools": []}),
    ])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    client.list_tools(timeout=20.0)
    sent = [json.loads(line) for line in backend.calls[0]["stdin"].splitlines()
            if line.strip()]
    assert [m.get("method") for m in sent] == [
        "initialize", "notifications/initialized", "tools/list"
    ]


def test_stdio_env_binds_to_the_server_not_to_printf():
    """`exec_argv` pipes stdin through printf and backends prepend `env K=V` to the
    whole string, where the pipe would bind it to printf. The env has to travel in
    the argv of the server itself."""
    backend = _StdioSandbox([_frame(1, {}), _frame("gc-tools/list", {"tools": []})])
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "stdio", "command": "srv-bin", "env": {"API_KEY": "k"}},
        os_backend=backend,
    )
    client.list_tools(timeout=20.0)
    call = backend.calls[0]
    assert call["argv"] == ["env", "API_KEY=k", "srv-bin"]
    # Passing it as a kwarg too would re-introduce the prefix-on-printf bug.
    assert call["env"] is None


def test_stdio_exchange_gets_the_whole_budget_and_no_more():
    backend = _StdioSandbox([_frame(1, {}), _frame("gc-tools/list", {"tools": []})])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    client.list_tools(timeout=20.0)
    assert backend.calls[0]["timeout"] == 20


def test_stdio_without_env_runs_the_command_bare():
    backend = _StdioSandbox([_frame(1, {}), _frame("gc-tools/list", {"tools": []})])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    client.list_tools(timeout=20.0)
    assert backend.calls[0]["argv"] == ["srv-bin"]


def test_stdio_call_tool_matches_the_reply_by_id():
    backend = _StdioSandbox([
        _frame(1, {}),
        '{"jsonrpc": "2.0", "method": "notifications/message"}',
        _frame("gc-tools/call", {"content": [{"type": "text", "text": "ok"}]}),
    ])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    out = client.call_tool("echo", {"text": "hi"}, timeout=20.0)
    assert out["result"]["content"][0]["text"] == "ok"


def test_stdio_missing_command_is_an_error():
    backend = _StdioSandbox([])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio"}, os_backend=backend
    )
    with pytest.raises(McpExecCarrierError) as excinfo:
        client.list_tools(timeout=20.0)
    assert "command" in str(excinfo.value)


def test_stdio_no_matching_reply_is_an_error():
    backend = _StdioSandbox([_frame(1, {}), "not json at all"])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    with pytest.raises(McpExecCarrierError):
        client.list_tools(timeout=20.0)


def test_stdio_truncated_output_is_an_error():
    class _TruncatingSandbox(_StdioSandbox):
        def exec_argv(self, argv, *, stdin=None, cwd=None, timeout=None, env=None,
                      capture_max_chars=None):
            return _ExecResult(stdout=_frame(1, {}), stdout_hit_capture_cap=True)

    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"},
        os_backend=_TruncatingSandbox([]),
    )
    with pytest.raises(McpExecCarrierError) as excinfo:
        client.list_tools(timeout=20.0)
    assert "truncated" in str(excinfo.value).lower()


def test_stdio_never_probes_the_http_carrier():
    """The HTTP carrier probe is irrelevant to stdio and must not run."""
    probes = []

    class _NoHttpSandbox(_StdioSandbox):
        def exec(self, command, *, cwd=None, timeout=None, env=None):
            probes.append(command)
            return _ExecResult(stdout="")

    backend = _NoHttpSandbox([_frame(1, {}), _frame("gc-tools/list", {"tools": []})])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    client.list_tools(timeout=20.0)
    assert probes == []


def test_stdio_carrier_uses_internal_channel_ceiling():
    backend = _StdioSandbox([
        _frame(1, {"protocolVersion": "2024-11-05"}),
        _frame("gc-tools/list", {"tools": []}),
    ])
    backend.walk_output_max_chars = 12_345_678
    client = OsBackendStatelessClient(
        "srv", {"transport": "stdio", "command": "srv-bin"}, os_backend=backend
    )
    client.list_tools(timeout=20.0)
    assert backend.calls[-1]["capture_max_chars"] == 12_345_678
