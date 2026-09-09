"""One test per acceptance line in .claude/specs/mcp-execute-on-stateless.SPEC.md."""
import json

import pytest


def _mcp():
    """Import the MCP layer fresh, inside the test.

    conftest installs an autouse fixture that reloads every `tools.*` module
    between tests, so classes captured by a module-level import here would come
    from a stale module object — `isinstance` and `pytest.raises` would then
    silently miss against the identically-named class the code actually returns.
    """
    from gc_core.tools.connect_mcp import client_port, execute_on, os_backend_client

    return client_port, execute_on, os_backend_client


class _ExecResult:
    def __init__(self, stdout="", stderr="", return_code=0,
                 stdout_hit_capture_cap=False):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.timed_out = False
        self.stdout_hit_capture_cap = stdout_hit_capture_cap
        self.stderr_hit_capture_cap = False


class _Sandbox:
    is_sandbox = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.codes = []
        self.execs = []

    def run_python(self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
        _, _, obc = _mcp()
        self.codes.append(code)
        return _ExecResult(
            stdout=obc.EXEC_RESULT_MARKER + json.dumps(self.answers.pop(0))
        )

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.execs.append(command)
        return _ExecResult(stdout="/usr/bin/python3")


class _Local:
    is_sandbox = False

    def __init__(self):
        self.execs = []

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.execs.append(command)
        return _ExecResult()

    def run_python(self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
        self.execs.append(code)
        return _ExecResult()


def _ok(body, headers=None):
    return {"status": 200, "headers": headers or {}, "body": body}


def test_no_execute_on_behaves_as_today():
    cp, eo, obc = _mcp()
    client = eo.resolve_mcp_client(
        "srv", {"transport": "stream_http", "url": "http://x"},
        os_backend=_Sandbox([]),
    )
    assert isinstance(client, cp.AgentRuntimeClient)


def test_sidecar_tool_is_listed_and_callable_and_sessions_are_not_reused():
    cp, eo, obc = _mcp()
    backend = _Sandbox([
        _ok({"result": {}}, {"mcp-session-id": "sid-1"}),
        _ok({}),
        _ok({"result": {"tools": [{"name": "ask_user"}]}}),
        _ok({"result": {}}, {"mcp-session-id": "sid-2"}),
        _ok({}),
        _ok({"result": {"content": [{"type": "text", "text": "answer"}]}}),
    ])
    client = eo.resolve_mcp_client(
        "user_server",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp",
         "execute_on": "os_backend"},
        os_backend=backend,
    )
    assert client.list_tools(timeout=30.0) == [{"name": "ask_user"}]
    out = client.call_tool("ask_user", {"question": "q"}, timeout=30.0)
    assert out["result"]["content"][0]["text"] == "answer"
    assert "sid-1" not in backend.codes[5]


def test_stdio_process_starts_in_the_backend_not_on_the_agent_host():
    cp, eo, obc = _mcp()
    calls = []

    class _StdioSandbox:
        is_sandbox = True

        def exec_argv(self, argv, *, stdin=None, cwd=None, timeout=None, env=None, capture_max_chars=None):
            calls.append(argv)
            return _ExecResult(stdout="\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
                json.dumps({"jsonrpc": "2.0", "id": "gc-tools/list",
                            "result": {"tools": []}}),
            ]))

        def exec(self, command, *, cwd=None, timeout=None, env=None):
            return _ExecResult(stdout="/usr/bin/python3")

    client = eo.resolve_mcp_client(
        "srv",
        {"transport": "stdio", "command": "srv-bin", "execute_on": "os_backend"},
        os_backend=_StdioSandbox(),
    )
    client.list_tools(timeout=20.0)
    assert calls == [["srv-bin"]]


@pytest.mark.parametrize("transport", ["sse", "stream_sse", "stream_stdio"])
def test_session_bound_transports_error_instead_of_falling_back(transport):
    cp, eo, obc = _mcp()
    with pytest.raises(eo.McpExecuteOnError):
        eo.resolve_mcp_client(
            "srv",
            {"transport": transport, "url": "http://x", "command": "c",
             "execute_on": "os_backend"},
            os_backend=_Sandbox([]),
        )


def test_same_config_on_local_backend_uses_the_agent_path_without_exec():
    cp, eo, obc = _mcp()
    local = _Local()
    client = eo.resolve_mcp_client(
        "srv",
        {"transport": "stream_http", "url": "http://x", "execute_on": "os_backend"},
        os_backend=local,
    )
    assert isinstance(client, cp.AgentRuntimeClient)
    assert local.execs == []


def test_agent_locus_server_details_never_enter_exec():
    cp, eo, obc = _mcp()
    backend = _Sandbox([
        _ok({"result": {}}, {"mcp-session-id": "sid"}),
        _ok({}),
        _ok({"result": {"tools": []}}),
    ])
    eo.resolve_mcp_client(
        "corp",
        {"transport": "stream_http", "url": "https://corp-internal.example/mcp",
         "token": "corp-secret", "execute_on": "agent"},
        os_backend=backend,
    )
    sidecar = eo.resolve_mcp_client(
        "sidecar",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp",
         "execute_on": "os_backend"},
        os_backend=backend,
    )
    sidecar.list_tools(timeout=10.0)
    joined = "\n".join(backend.codes)
    assert "corp-secret" not in joined
    assert "corp-internal.example" not in joined


def test_missing_carrier_is_an_error_and_installs_nothing():
    cp, eo, obc = _mcp()
    class _BareSandbox:
        is_sandbox = True

        def __init__(self):
            self.execs = []

        def exec(self, command, *, cwd=None, timeout=None, env=None):
            self.execs.append(command)
            return _ExecResult(stdout="", return_code=1)

        def run_python(self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
            raise AssertionError("must not run python3 when it is absent")

    backend = _BareSandbox()
    client = eo.resolve_mcp_client(
        "srv",
        {"transport": "http", "url": "http://x/mcp", "execute_on": "os_backend"},
        os_backend=backend,
    )
    with pytest.raises(obc.McpExecCarrierError):
        client.list_tools(timeout=10.0)
    assert not any("install" in cmd for cmd in backend.execs)


def test_truncated_exec_output_is_an_error():
    cp, eo, obc = _mcp()
    class _TruncatingSandbox:
        is_sandbox = True

        def run_python(self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
            _, _, obc = _mcp()
            return _ExecResult(
                stdout=obc.EXEC_RESULT_MARKER + '{"status": 200, "bo',
                stdout_hit_capture_cap=True,
            )

        def exec(self, command, *, cwd=None, timeout=None, env=None):
            return _ExecResult(stdout="/usr/bin/python3")

    client = obc.OsBackendStatelessClient(
        "srv", {"transport": "http", "url": "http://x/mcp"},
        os_backend=_TruncatingSandbox(),
    )
    with pytest.raises(obc.McpExecCarrierError):
        client.list_tools(timeout=10.0)


def test_tls_material_on_os_backend_is_a_config_error():
    cp, eo, obc = _mcp()
    with pytest.raises(eo.McpExecuteOnError):
        eo.resolve_mcp_client(
            "srv",
            {"transport": "stream_http", "url": "https://x",
             "execute_on": "os_backend", "client-cert": "/etc/ssl/c.pem"},
            os_backend=_Sandbox([]),
        )


def test_mixed_config_keeps_both_servers_alive():
    cp, eo, obc = _mcp()
    backend = _Sandbox([
        _ok({"result": {}}, {"mcp-session-id": "sid"}),
        _ok({}),
        _ok({"result": {"tools": [{"name": "ask_user"}]}}),
    ])
    agent_client = eo.resolve_mcp_client(
        "corp", {"transport": "stream_http", "url": "https://corp/mcp"},
        os_backend=backend,
    )
    sidecar_client = eo.resolve_mcp_client(
        "sidecar",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp",
         "execute_on": "os_backend"},
        os_backend=backend,
    )
    assert isinstance(agent_client, cp.AgentRuntimeClient)
    assert isinstance(sidecar_client, obc.OsBackendStatelessClient)
    assert sidecar_client.list_tools(timeout=10.0) == [{"name": "ask_user"}]
