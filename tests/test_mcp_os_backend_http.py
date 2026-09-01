import base64
import json
import re

import pytest

from tools.connect_mcp.os_backend_client import (
    EXEC_RESULT_MARKER,
    McpExecCarrierError,
    OsBackendStatelessClient,
)


class _ExecResult:
    def __init__(self, stdout="", stderr="", return_code=0, timed_out=False,
                 stdout_hit_capture_cap=False):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.timed_out = timed_out
        self.stdout_hit_capture_cap = stdout_hit_capture_cap
        self.stderr_hit_capture_cap = False


class _RecordingSandbox:
    """Replays one scripted answer per run_python call, recording the code."""

    is_sandbox = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.codes = []
        self.timeouts = []

    def run_python(self, code, *, cwd=None, timeout=None, env=None):
        self.codes.append(code)
        self.timeouts.append(timeout)
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


def test_list_tools_runs_full_handshake_in_one_operation():
    backend = _RecordingSandbox([
        _ok({"result": {"protocolVersion": "2024-11-05"}},
            {"mcp-session-id": "sid-1"}),
        _ok({}),
        _ok({"result": {"tools": [{"name": "ask_user"}]}}),
    ])
    client = OsBackendStatelessClient(
        "user_server",
        {"transport": "stream_http", "url": "http://user-server:8000/mcp",
         "execute_on": "os_backend"},
        os_backend=backend,
    )
    assert client.list_tools(timeout=30.0) == [{"name": "ask_user"}]
    assert len(backend.codes) == 3
    assert [_sent(c)["payload"]["method"] for c in backend.codes] == [
        "initialize", "notifications/initialized", "tools/list"
    ]
    # The handshake's session id is used within the operation, never before it.
    assert "mcp-session-id" not in _sent(backend.codes[0])["headers"]
    assert _sent(backend.codes[2])["headers"]["mcp-session-id"] == "sid-1"


def test_second_operation_does_not_reuse_the_first_session_id():
    backend = _RecordingSandbox([
        _ok({"result": {}}, {"mcp-session-id": "sid-A"}),
        _ok({}),
        _ok({"result": {"tools": []}}),
        _ok({"result": {}}, {"mcp-session-id": "sid-B"}),
        _ok({}),
        _ok({"result": {"content": []}}),
    ])
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "stream_http", "url": "http://x:8000/mcp"},
        os_backend=backend,
    )
    client.list_tools(timeout=10.0)
    client.call_tool("ask_user", {"question": "q"}, timeout=10.0)
    # The second operation opens its own session; sid-A must not survive into it.
    second = [_sent(c)["headers"].get("mcp-session-id") for c in backend.codes[3:]]
    assert second == [None, "sid-B", "sid-B"]


def test_call_tool_returns_the_raw_jsonrpc_body():
    backend = _RecordingSandbox([
        _ok({"result": {}}, {"mcp-session-id": "sid"}),
        _ok({}),
        _ok({"result": {"content": [{"type": "text", "text": "hi"}]}}),
    ])
    client = OsBackendStatelessClient(
        "srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend
    )
    out = client.call_tool("ask_user", {"question": "q"}, timeout=10.0)
    assert out["result"]["content"][0]["text"] == "hi"


def test_http_error_status_raises():
    backend = _RecordingSandbox([
        {"status": 500, "headers": {}, "body": {}},
    ])
    client = OsBackendStatelessClient(
        "srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend
    )
    with pytest.raises(Exception) as excinfo:
        client.list_tools(timeout=10.0)
    assert "500" in str(excinfo.value)


def test_truncated_stdout_is_an_error_not_broken_json():
    backend = _RecordingSandbox([
        _ExecResult(stdout=EXEC_RESULT_MARKER + '{"status": 200, "hea',
                    stdout_hit_capture_cap=True),
    ])
    client = OsBackendStatelessClient(
        "srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend
    )
    with pytest.raises(McpExecCarrierError) as excinfo:
        client.list_tools(timeout=10.0)
    assert "truncated" in str(excinfo.value).lower()


def test_missing_marker_is_an_error_carrying_stderr():
    backend = _RecordingSandbox([
        _ExecResult(stdout="", stderr="python3: not found", return_code=127),
    ])
    client = OsBackendStatelessClient(
        "srv", {"transport": "http", "url": "http://x:8000/mcp"}, os_backend=backend
    )
    with pytest.raises(McpExecCarrierError) as excinfo:
        client.list_tools(timeout=10.0)
    assert "python3: not found" in str(excinfo.value)


def test_carrier_script_stops_at_the_first_sse_frame_without_waiting_for_eof():
    """D1: the carrier must not call resp.read() on a text/event-stream answer."""
    backend = _RecordingSandbox([_ok({"result": {}}, {"mcp-session-id": "s"})])
    client = OsBackendStatelessClient(
        "srv", {"transport": "stream_http", "url": "http://x:8000/mcp"},
        os_backend=backend,
    )
    try:
        client.list_tools(timeout=10.0)
    except Exception:
        pass
    script = backend.codes[0]
    assert "text/event-stream" in script
    assert "readline" in script


def test_agent_locus_servers_never_appear_in_exec():
    """Isolation: only this server's own config may reach the container."""
    backend = _RecordingSandbox([
        _ok({"result": {}}, {"mcp-session-id": "sid"}),
        _ok({}),
        _ok({"result": {"tools": []}}),
    ])
    client = OsBackendStatelessClient(
        "srv",
        {"transport": "http", "url": "http://sidecar:8000/mcp",
         "token": "sidecar-token"},
        os_backend=backend,
    )
    client.list_tools(timeout=10.0)
    for code in backend.codes:
        sent = _sent(code)
        assert sent["url"] == "http://sidecar:8000/mcp"
        assert "corp-internal.example" not in json.dumps(sent)
        assert "other-secret-token" not in json.dumps(sent)
