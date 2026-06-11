import time
import tools.connect_mcp.registry as reg


def test_build_registry_parallel_and_status(monkeypatch):
    def fake_discover_one(name, cfg, *, timeout):
        if name == "ok":
            return ([{"name": "do_x", "description": "d"}],
                    {"status": "connected", "reason": None, "tool_count": 1, "elapsed_ms": 5})
        time.sleep(timeout + 2)  # «зависание» дольше дедлайна — не должны его ждать
        return ([], {"status": "connected", "reason": None, "tool_count": 0, "elapsed_ms": 0})

    monkeypatch.setattr(reg, "_discover_one", fake_discover_one)
    servers = {"ok": {"transport": "stdio", "command": "x"},
               "hang": {"transport": "stdio", "command": "y"}}

    t0 = time.monotonic()
    tools, status = reg._build_tools_registry(servers, discovery_timeout_sec=1)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5
    assert status["ok"]["status"] == "connected"
    assert status["hang"]["status"] == "failed"
    assert "таймаут" in (status["hang"]["reason"] or "")
    assert any(k.startswith("mcp_ok_") for k in tools)


def test_discover_one_bad_command_failed_fast():
    t0 = time.monotonic()
    tools, status = reg._discover_one(
        "office", {"transport": "stdio", "command": "definitely-not-a-real-cmd-xyz",
                   "args": ["--x"]},
        timeout=8,
    )
    assert time.monotonic() - t0 < 5
    assert tools == []
    assert status["status"] == "failed"
    assert "не найдена" in (status["reason"] or "")


def test_humanize_unwraps_exception_group():
    # stream_http (anyio) оборачивает реальную ошибку в ExceptionGroup/TaskGroup —
    # причина должна разворачиваться до внутренней (человекочитаемой).
    class ConnectError(Exception):
        pass
    inner = ConnectError("Connection refused")
    eg = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    reason = reg._humanize_discovery_error(eg, {"url": "http://localhost:8931/mcp"})
    assert "нет соединения" in reason
    assert "localhost:8931" in reason
    assert "TaskGroup" not in reason
