import base64
import json
import re

import pytest
from gc_core.tools.connect_mcp.os_backend_client import (
    _REQUESTS_PER_OPERATION,
    EXEC_RESULT_MARKER,
    McpExecCarrierError,
    OsBackendStatelessClient,
    _split_share,
)


class _ExecResult:
    def __init__(self, stdout="", stderr="", return_code=0, timed_out=False, stdout_hit_capture_cap=False):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.timed_out = timed_out
        self.stdout_hit_capture_cap = stdout_hit_capture_cap
        self.stderr_hit_capture_cap = False


class _RecordingSandbox:
    """Replays one scripted answer per carrier exec, recording the script.

    The carrier is ``python3 -`` fed via ``exec_argv`` stdin — the same channel
    the stdio transport uses — so ``run_python`` (the model-facing python tool)
    must never be touched.
    """

    is_sandbox = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.codes = []
        self.argvs = []
        self.timeouts = []
        self.capture_caps = []

    def exec_argv(self, argv, *, stdin=None, cwd=None, timeout=None, env=None, capture_max_chars=None):
        self.argvs.append(list(argv))
        self.codes.append(stdin)
        self.timeouts.append(timeout)
        self.capture_caps.append(capture_max_chars)
        payload = self.answers.pop(0)
        if isinstance(payload, _ExecResult):
            return payload
        return _ExecResult(stdout=EXEC_RESULT_MARKER + json.dumps(payload))

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        # Answers the carrier probe Task 6 adds, so these tests keep passing then.
        return _ExecResult(stdout="/usr/bin/python3")


def _ok(body, headers=None):
    return {"status": 200, "headers": headers or {}, "body": body}


def _sent(code):
    """The request the carrier script carries, decoded out of its base64 blob."""
    match = re.search(r'b64decode\("([^"]+)"\)', code)
    assert match, "carrier script carries no encoded request"
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))


async def test_list_tools_runs_full_handshake_in_one_operation():
    backend = _RecordingSandbox(
        [
            _ok({"result": {"protocolVersion": "2024-11-05"}}, {"mcp-session-id": "sid-1"}),
            _ok({}),
            _ok({"result": {"tools": [{"name": "ask_user"}]}}),
        ]
    )
    client = OsBackendStatelessClient(
        "user_server",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp", "execute_on": "os_backend"},
        os_backend=backend,
    )
    assert await client.alist_tools(timeout=30.0) == [{"name": "ask_user"}]
    assert len(backend.codes) == 3
    assert [_sent(c)["payload"]["method"] for c in backend.codes] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    # The handshake's session id is used within the operation, never before it.
    assert "mcp-session-id" not in _sent(backend.codes[0])["headers"]
    assert _sent(backend.codes[2])["headers"]["mcp-session-id"] == "sid-1"


async def test_second_operation_does_not_reuse_the_first_session_id():
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "sid-A"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
            _ok({"result": {}}, {"mcp-session-id": "sid-B"}),
            _ok({}),
            _ok({"result": {"content": []}}),
        ]
    )
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "stream_http", "url": "http://x:8000/mcp"},
        os_backend=backend,
    )
    await client.alist_tools(timeout=10.0)
    await client.acall_tool("ask_user", {"question": "q"}, timeout=10.0)
    # The second operation opens its own session; sid-A must not survive into it.
    second = [_sent(c)["headers"].get("mcp-session-id") for c in backend.codes[3:]]
    assert second == [None, "sid-B", "sid-B"]


async def test_call_tool_returns_the_raw_jsonrpc_body():
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "sid"}),
            _ok({}),
            _ok({"result": {"content": [{"type": "text", "text": "hi"}]}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    out = await client.acall_tool("ask_user", {"question": "q"}, timeout=10.0)
    assert out["result"]["content"][0]["text"] == "hi"


async def test_http_error_status_raises():
    backend = _RecordingSandbox(
        [
            {"status": 500, "headers": {}, "body": {}},
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    with pytest.raises(Exception) as excinfo:
        await client.alist_tools(timeout=10.0)
    assert "500" in str(excinfo.value)


async def test_truncated_stdout_is_an_error_not_broken_json():
    backend = _RecordingSandbox(
        [
            _ExecResult(stdout=EXEC_RESULT_MARKER + '{"status": 200, "hea', stdout_hit_capture_cap=True),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    with pytest.raises(McpExecCarrierError) as excinfo:
        await client.alist_tools(timeout=10.0)
    assert "truncated" in str(excinfo.value).lower()


async def test_missing_marker_is_an_error_carrying_stderr():
    backend = _RecordingSandbox(
        [
            _ExecResult(stdout="", stderr="python3: not found", return_code=127),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    with pytest.raises(McpExecCarrierError) as excinfo:
        await client.alist_tools(timeout=10.0)
    assert "python3: not found" in str(excinfo.value)


async def test_carrier_script_stops_at_the_first_sse_frame_without_waiting_for_eof():
    """D1: the carrier must not call resp.read() on a text/event-stream answer."""
    backend = _RecordingSandbox([_ok({"result": {}}, {"mcp-session-id": "s"})])
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "stream_http", "url": "http://x:8000/mcp"},
        os_backend=backend,
    )
    try:
        await client.alist_tools(timeout=10.0)
    except Exception:
        pass
    script = backend.codes[0]
    assert "text/event-stream" in script
    assert "readline" in script


async def test_agent_locus_servers_never_appear_in_exec():
    """Isolation: only this server's own config may reach the container."""
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "sid"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "http", "url": "http://sidecar:8000/mcp", "token": "sidecar-token"},
        os_backend=backend,
    )
    await client.alist_tools(timeout=10.0)
    for code in backend.codes:
        sent = _sent(code)
        assert sent["url"] == "http://sidecar:8000/mcp"
        assert "corp-internal.example" not in json.dumps(sent)
        assert "other-secret-token" not in json.dumps(sent)


class _ProbingSandbox(_RecordingSandbox):
    """Sandbox whose PATH may or may not have python3."""

    def __init__(self, answers, *, has_python3):
        super().__init__(answers)
        self.has_python3 = has_python3
        self.probes = []

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.probes.append(command)
        return _ExecResult(stdout="/usr/bin/python3" if self.has_python3 else "")


async def test_python3_present_lets_the_operation_run():
    backend = _ProbingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "s"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ],
        has_python3=True,
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    assert await client.alist_tools(timeout=10.0) == []


async def test_missing_python3_is_an_explicit_error_and_installs_nothing():
    backend = _ProbingSandbox([], has_python3=False)
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    with pytest.raises(McpExecCarrierError) as excinfo:
        await client.alist_tools(timeout=10.0)
    assert "python3" in str(excinfo.value)
    assert "execute_on: agent" in str(excinfo.value)
    assert not any("install" in probe for probe in backend.probes)
    # No shell carrier exists any more, so nothing was attempted over exec.
    assert backend.codes == []


async def test_carrier_probe_is_cached_on_the_backend_not_the_client():
    """Clients are rebuilt per operation; the probe must not be."""
    backend = _ProbingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "s"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
            _ok({"result": {}}, {"mcp-session-id": "s"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ],
        has_python3=True,
    )
    cfg = {"transport": "http", "url": "http://x:8000/mcp"}
    await OsBackendStatelessClient("srv", cfg, os_backend=backend).alist_tools(timeout=10.0)
    await OsBackendStatelessClient("srv", cfg, os_backend=backend).alist_tools(timeout=10.0)
    assert len([p for p in backend.probes if "command -v" in p]) == 1


async def test_the_operation_fits_inside_the_budget_it_was_given():
    """Three exec deadlines must not add up past the caller's timeout — that is
    the deadline registry uses to decide the server is dead."""
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "s"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=30.0)
    assert backend.timeouts == [10, 10, 10]
    assert sum(backend.timeouts) <= 30


async def test_socket_timeout_ends_before_the_exec_kill():
    """Ordering inside a share: the in-script socket gives up first, so a slow
    server is reported as an MCP timeout, not an opaque exec one."""
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "s"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=30.0)
    for code, deadline in zip(backend.codes, backend.timeouts):
        assert _sent(code)["timeout"] < deadline


@pytest.mark.parametrize("total", [3, 8, 12, 20, 30, 45, 90, 120, 300, 600])
def test_the_ordering_holds_on_every_budget(total):
    """Not only on budgets that divide evenly: the margin comes off the integer
    deadline, so truncation cannot leave the socket outliving the exec kill."""
    socket_timeout, deadline = _split_share(max(1.0, total / _REQUESTS_PER_OPERATION))
    assert socket_timeout <= deadline
    if deadline >= 2:
        assert socket_timeout < deadline


async def test_slack_keeps_its_full_size_when_the_budget_allows():
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "s"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=600.0)
    assert backend.timeouts == [200, 200, 200]
    assert _sent(backend.codes[0])["timeout"] == 185.0


async def test_final_request_asks_the_server_to_drop_the_session():
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "sid-1"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=10.0)
    assert [_sent(c)["close_session"] for c in backend.codes] == [False, False, True]
    assert "DELETE" in backend.codes[2]


async def test_delete_carries_the_same_auth_headers_as_the_posts():
    """A token-protected server answers a bare DELETE with 401 and the session
    outlives the operation. The carrier must reuse the request headers."""
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "sid-1"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "http", "url": "http://x:8000/mcp", "token": "t0k", "headers": {"X-Team": "bench"}},
        os_backend=backend,
    )
    await client.alist_tools(timeout=10.0)
    final = _sent(backend.codes[2])
    assert final["headers"]["Authorization"] == "Bearer t0k"
    assert final["headers"]["X-Team"] == "bench"
    assert final["headers"]["mcp-session-id"] == "sid-1"
    # The DELETE is built from the same header dict, not from a bare session id.
    delete_src = backend.codes[2].split("def _delete_session")[1].split("\ntry:")[0]
    assert 'dict(_req["headers"])' in delete_src
    assert '{"mcp-session-id": session_id}' not in delete_src


async def test_the_answer_is_printed_before_the_session_is_released():
    """Releasing the session is best effort, so it must not be able to lose the
    answer: a server that accepts the call and then wedges on DELETE would eat
    the exec deadline, and an unflushed envelope dies with the killed process."""
    backend = _RecordingSandbox(
        [
            _ok({"result": {}}, {"mcp-session-id": "sid-1"}),
            _ok({}),
            _ok({"result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=10.0)
    code = backend.codes[2]
    printed = code.index('json.dumps({"status"')
    released = code.index("\n        _delete_session(url, session_id)")
    assert printed < released
    assert "sys.stdout.flush()" in code[printed:released]


async def test_verify_ssl_false_reaches_the_carrier():
    backend = _RecordingSandbox([_ok({"result": {}}, {"mcp-session-id": "s"})])
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "http", "url": "https://x:8000/mcp", "verify-ssl": False},
        os_backend=backend,
    )
    try:
        await client.alist_tools(timeout=10.0)
    except Exception:
        pass
    assert _sent(backend.codes[0])["verify_ssl"] is False
    assert "_create_unverified_context" in backend.codes[0]


async def test_verify_ssl_defaults_to_on():
    backend = _RecordingSandbox([_ok({"result": {}}, {"mcp-session-id": "s"})])
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "https://x:8000/mcp"}, os_backend=backend)
    try:
        await client.alist_tools(timeout=10.0)
    except Exception:
        pass
    assert _sent(backend.codes[0])["verify_ssl"] is True


async def test_only_307_308_are_followed_by_hand():
    """urllib follows 301/302 itself (rewriting POST to GET); only 307/308 reach us."""
    backend = _RecordingSandbox([_ok({"result": {}}, {"mcp-session-id": "s"})])
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    try:
        await client.alist_tools(timeout=10.0)
    except Exception:
        pass
    script = backend.codes[0]
    assert "(307, 308)" in script
    assert "301" not in script.split("def _post")[0].split("_read_json_frame")[-1]


async def test_unresolved_redirect_is_an_error_not_a_hollow_success():
    """Four hops without settling must not look like `connected, 0 tools`."""
    backend = _RecordingSandbox(
        [
            {"error": "RuntimeError: redirect loop not resolved after 4 hops: http://x/"},
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    with pytest.raises(McpExecCarrierError) as excinfo:
        await client.alist_tools(timeout=10.0)
    assert "redirect loop" in str(excinfo.value)


async def test_no_scratch_file_is_written_in_the_container():
    """The module docstring promises no file is written; hold it to that."""
    backend = _RecordingSandbox([_ok({"result": {}}, {"mcp-session-id": "s"})])
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    try:
        await client.alist_tools(timeout=10.0)
    except Exception:
        pass
    assert "/tmp/" not in backend.codes[0]


async def test_carrier_runs_python3_from_stdin_via_exec_argv():
    backend = _RecordingSandbox(
        [
            _ok({"jsonrpc": "2.0", "id": 1, "result": {}}),
            {"status": 202, "headers": {}, "body": ""},
            _ok({"jsonrpc": "2.0", "id": "gc-tools/list", "result": {"tools": []}}),
        ]
    )
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=10.0)
    assert backend.argvs and all(argv == ["python3", "-"] for argv in backend.argvs)
    assert all(EXEC_RESULT_MARKER in code for code in backend.codes)


async def test_carrier_uses_internal_channel_ceiling_not_tool_trace_cap():
    """The MCP answer is transport, not a model-facing trace: it must ride the
    backend's walk ceiling (50M) instead of inheriting the 60k shell cap."""
    backend = _RecordingSandbox(
        [
            _ok({"jsonrpc": "2.0", "id": 1, "result": {}}),
            {"status": 202, "headers": {}, "body": ""},
            _ok({"jsonrpc": "2.0", "id": "gc-tools/list", "result": {"tools": []}}),
        ]
    )
    backend.walk_output_max_chars = 12_345_678
    client = OsBackendStatelessClient("srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend)
    await client.alist_tools(timeout=10.0)
    assert backend.capture_caps
    assert all(cap == 12_345_678 for cap in backend.capture_caps)


# --- асинхронный путь ---------------------------------------------------------
#
# Обмен идёт одним экзеком в бэкенде задачи, и у экзека есть асинхронная форма:
# своя у `local.py`, адаптер `aexec_port` для чужих бэкендов. Раньше здесь
# стоял `run_abandonable` — поток-демон на каждый вызов тула.


class _AsyncSandbox(_RecordingSandbox):
    """Бэкенд, объявивший асинхронный порт — как `local.py` и `HarborBackend`."""

    async def arun_python(self, code, *, cwd=None, timeout=None, env=None):
        return self.run_python(code, cwd=cwd, timeout=timeout, env=env)

    async def aexec(self, command, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
        return self.exec(command, cwd=cwd, timeout=timeout, env=env)

    async def aexec_argv(self, argv, *, stdin=None, cwd=None, timeout=None, env=None, capture_max_chars=None):
        # HTTP-носитель ходит именно сюда: `python3 -` со скриптом на stdin —
        # тот же канал, что у stdio-транспорта. Раньше здесь стоял
        # `AssertionError`: носитель шёл через `run_python`, и это утверждение
        # было верным до правки develop «route http carrier via exec_argv»
        # (у модельного `run_python` свой потолок вывода, рассчитанный на
        # трассу тула, и крупный ответ MCP он обрезал).
        return self.exec_argv(argv, stdin=stdin, cwd=cwd, timeout=timeout, env=env, capture_max_chars=capture_max_chars)


async def test_async_operation_takes_no_thread():
    """У бэкенда с асинхронным портом обмен идёт прямым await, без потоков.

    Раньше здесь стоял `run_abandonable` — поток-демон на КАЖДЫЙ вызов тула,
    независимо от того, умеет бэкенд асинхронно или нет.
    """
    import threading

    backend = _AsyncSandbox(
        [
            _ok({"result": {"protocolVersion": "2024-11-05"}}, {"mcp-session-id": "sid-1"}),
            _ok({}),
            _ok({"result": {"tools": [{"name": "ask_user"}]}}),
        ]
    )
    client = OsBackendStatelessClient(
        "user_server",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp"},
        os_backend=backend,
    )

    # Считаем СОЗДАНИЕ потоков, а не живые: поток-демон `run_abandonable`
    # успевает умереть до замера, и `active_count()` возвращается к базе —
    # проверено подменой, такой зонд регрессию пропускал.
    spawned: list = []
    real_start = threading.Thread.start

    def _counting_start(self):
        spawned.append(self.name)
        return real_start(self)

    threading.Thread.start = _counting_start
    try:
        tools = await client.alist_tools(timeout=30)
    finally:
        threading.Thread.start = real_start

    assert [t["name"] for t in tools] == ["ask_user"]
    assert spawned == [], f"асинхронный обмен завёл потоки {spawned} — путь снова через run_abandonable или пул"


async def test_sync_backend_still_works_through_the_adapter():
    """Бэкенд БЕЗ асинхронной формы продолжает работать — через `SyncExecAdapter`.

    Это обещание встраивающим: свой `FsBackend` переезда на asyncio не заметил.
    Поток здесь законен и является ценой совместимости, а не недоделкой.
    """
    backend = _RecordingSandbox(
        [
            _ok({"result": {"protocolVersion": "2024-11-05"}}, {"mcp-session-id": "sid-1"}),
            _ok({}),
            _ok({"result": {"tools": [{"name": "ask_user"}]}}),
        ]
    )
    client = OsBackendStatelessClient(
        "user_server",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp"},
        os_backend=backend,
    )

    tools = await client.alist_tools(timeout=30)
    assert [t["name"] for t in tools] == ["ask_user"]
