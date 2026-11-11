"""Тестовая инфраструктура MCP: серверы, помощники, фикстуры, подставные часы.

На ней стоят тесты жизненного цикла MCP-сессий, поэтому здесь проверено, что
серверы ведут себя ровно как обещают их ключи поведения и журнал. Голый
обмен (``_raw``) говорит с процессом напрямую, без клиента ядра: так видно
поведение самого сервера, а не то, как его прочитал клиент.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from gc_core.config_modules import env_vars
from gc_core.execution.tool_call_context import bind_tool_call
from gc_core.mcp_session_owner import current_owner
from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient
from gc_core.user_stop import UserStop, UserStopInterrupt, bind_user_stop
from gc_testkit.mcp import (
    FakeClock,
    alive,
    call_tool,
    children,
    events,
    exits,
    free_port,
    launches,
    set_behavior,
    stdio_cfg,
    stream_server,
    wait_for,
)
from gc_testkit.mcp import fixtures as mcp_fixtures
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

_TIMEOUT = 5.0


@contextlib.asynccontextmanager
async def _raw(cfg: dict):
    """Процесс сервера из определения, лидер своей группы, без клиента ядра."""
    proc = await asyncio.create_subprocess_exec(
        cfg["command"],
        *cfg["args"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, **cfg["env"]},
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        await asyncio.wait_for(proc.wait(), _TIMEOUT)


async def _send(proc, message: dict) -> None:
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()


async def _line(proc) -> str:
    raw = await asyncio.wait_for(proc.stdout.readline(), _TIMEOUT)
    return raw.decode().strip()


async def _reply(proc, rid) -> dict | None:
    """Ответ с этим id; ``None`` — сервер закрыл вывод, не ответив."""
    while True:
        line = await _line(proc)
        if not line and proc.stdout.at_eof():
            return None
        with contextlib.suppress(ValueError):
            message = json.loads(line)
            if (
                isinstance(message, dict)
                and message.get("id") == rid
                and "method" not in message
            ):
                return message


async def _rpc(proc, rid, method: str, params: dict | None = None):
    await _send(
        proc,
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
    )
    return await _reply(proc, rid)


async def _initialize(proc) -> dict:
    reply = await _rpc(
        proc,
        0,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}},
    )
    assert reply is not None
    return reply["result"]


def _names(result: dict) -> list[str]:
    return [tool["name"] for tool in result["tools"]]


def _tool_out(reply: dict) -> dict:
    return json.loads(reply["result"]["content"][0]["text"])


class _NullLog:
    def info(self, *args, **kwargs) -> None:
        del args, kwargs


# --- stdio-сервер через клиент ядра ---------------------------------------


async def test_server_lists_tools_and_logs_launch(
    tmp_path, mcp_owner, mcp_reaper
):
    control = tmp_path / "s.json"

    out = await call_tool("s", stdio_cfg(control), "echo", {"text": "x"})

    assert out["text"] == "x"
    assert len(launches(control)) >= 1
    assert out["pid"] in launches(control)


async def test_behavior_changes_without_changing_definition(
    tmp_path, mcp_owner, mcp_reaper
):
    control = tmp_path / "s.json"
    cfg = stdio_cfg(control)
    await call_tool("s", cfg, "echo", {"text": "x"})

    set_behavior(control, tools=["echo"])
    async with _raw(cfg) as proc:
        await _initialize(proc)
        listed = await _rpc(proc, 1, "tools/list")

    assert _names(listed["result"]) == ["echo"]
    assert cfg == stdio_cfg(control)


async def test_nap_is_cancelled_and_server_keeps_serving(
    tmp_path, mcp_owner, mcp_reaper
):
    control = tmp_path / "s.json"
    cfg = {**stdio_cfg(control), "persistent_session": True}
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), _NullLog())

    async def _nap():
        with bind_user_stop(gate), bind_tool_call("nap", "call-1"):
            return await call_tool("p", cfg, "nap", {"seconds": 30})

    task = asyncio.create_task(_nap())
    try:
        await wait_for(
            lambda: any(
                e.startswith("req tools/call") for e in events(control)
            )
        )
        gate.request()
        with pytest.raises(UserStopInterrupt):
            await asyncio.wait_for(task, _TIMEOUT)
        await wait_for(
            lambda: any(e.startswith("cancelled ") for e in events(control))
        )

        out = await call_tool("p", cfg, "echo", {"text": "y"}, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    assert out["pid"] == launches(control)[0]
    assert len(launches(control)) == 1


# --- stdio-сервер голым обменом -------------------------------------------


async def test_initialize_answer_is_accepted_by_the_sdk_client(
    tmp_path, mcp_owner, mcp_reaper
):
    control = tmp_path / "s.json"
    cfg = {**stdio_cfg(control), "transport": "stream_stdio"}

    tools = await AgentRuntimeClient("ss", cfg).alist_tools(timeout=10)
    out = await call_tool("ss", cfg, "echo", {"text": "z"})

    assert [tool["name"] for tool in tools] == ["echo", "nap"]
    assert all("inputSchema" in tool for tool in tools)
    assert out["text"] == "z"


async def test_initialize_echoes_the_protocol_version(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    async with _raw(stdio_cfg(control)) as proc:
        result = await _initialize(proc)

    assert result["protocolVersion"] == "2025-06-18"
    assert "capabilities" in result
    assert "serverInfo" in result


async def test_fail_start_then_recover(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    cfg = stdio_cfg(control)

    set_behavior(control, fail_start=True)
    async with _raw(cfg) as proc:
        await _send(proc, {"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        assert await _reply(proc, 0) is None
        assert await asyncio.wait_for(proc.wait(), _TIMEOUT) == 1

    set_behavior(control, fail_start=False)
    async with _raw(cfg) as proc:
        await _initialize(proc)
        listed = await _rpc(proc, 1, "tools/list")

    assert _names(listed["result"]) == ["echo", "nap"]
    assert len(launches(control)) == 2


async def test_start_delay_holds_the_initialize_answer(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=0.5)
    async with _raw(stdio_cfg(control)) as proc:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await _initialize(proc)

        assert loop.time() - started >= 0.5


async def test_exit_is_logged_on_eof_and_sigterm(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    cfg = stdio_cfg(control)

    async with _raw(cfg) as proc:
        await _initialize(proc)
        proc.stdin.close()
        assert await asyncio.wait_for(proc.wait(), _TIMEOUT) == 0
    async with _raw(cfg) as proc:
        await _initialize(proc)
        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), _TIMEOUT)

    assert len(launches(control)) == 2
    assert exits(control) == launches(control)


async def test_ignore_eof_keeps_serving_until_sigterm(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    set_behavior(control, ignore_eof=True)
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        proc.stdin.close()
        await asyncio.sleep(0.3)
        assert proc.returncode is None
        assert exits(control) == []
        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), _TIMEOUT)

    assert exits(control) == launches(control)


async def test_ignore_term_and_child(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    set_behavior(control, ignore_term=True, spawn_child=True)
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        [child] = children(control)

        proc.send_signal(signal.SIGTERM)
        # Ответ после сигнала: сигнал уже доставлен и проигнорирован.
        assert await _rpc(proc, 1, "tools/list") is not None
        assert alive(proc.pid)
        assert alive(child)
        assert exits(control) == []

        await mcp_reaper.reap()

        assert not alive(proc.pid)
        assert not alive(child)


async def test_child_ignores_term(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    set_behavior(control, child_ignores_term=True)
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        [child] = children(control)

        # Сигнал всей группе — как при закрытии сессии: лидер выходит,
        # глухой потомок остаётся (форма node + Chromium).
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), _TIMEOUT)

        assert not alive(proc.pid)
        assert alive(child)
        assert exits(control) == launches(control)


async def test_nap_does_not_block_other_requests(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        nap = {"name": "nap", "arguments": {"seconds": 30}}
        await _send(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": nap},
        )
        await wait_for(lambda: "req tools/call 1" in events(control))

        echo = {"name": "echo", "arguments": {"text": "a"}}
        reply = await _rpc(proc, 2, "tools/call", echo)
        assert _tool_out(reply)["text"] == "a"

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            },
        )
        await wait_for(lambda: "cancelled 1" in events(control))
        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": echo,
            },
        )
        # Ответа на снятый nap нет: следующая строка вывода — ответ id 3.
        assert json.loads(await _line(proc))["id"] == 3


async def test_empty_tools_and_missing_key(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        set_behavior(control, tools=[])
        empty = await _rpc(proc, 1, "tools/list")
        set_behavior(control, no_tools_key=True)
        missing = await _rpc(proc, 2, "tools/list")

    assert empty["result"] == {"tools": []}
    assert "tools" not in missing["result"]


async def test_exit_on_method_leaves_without_reply(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    set_behavior(control, exit_on="tools/list")
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        assert await _rpc(proc, 1, "tools/list") is None
        assert await asyncio.wait_for(proc.wait(), _TIMEOUT) == 1

    assert "req tools/list 1" in events(control)
    assert exits(control) == []


async def test_noise_and_notifications_come_before_each_reply(
    tmp_path, mcp_reaper
):
    control = tmp_path / "s.json"
    set_behavior(control, noise=True, notifications=2)
    async with _raw(stdio_cfg(control)) as proc:
        for rid, method in ((0, "initialize"), (1, "tools/list")):
            await _send(
                proc,
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": {}},
            )
            before = []
            line = await _line(proc)
            while "id" not in (message := _json_or_none(line) or {}):
                before.append(line)
                line = await _line(proc)

            assert message["id"] == rid
            assert any(_json_or_none(text) is None for text in before)
            notes = [
                text
                for text in before
                if (_json_or_none(text) or {}).get("method")
                == "notifications/message"
            ]
            assert len(notes) == 2


def _json_or_none(text: str):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def test_unknown_tool_is_an_error_and_crash_is_always_there(
    tmp_path, mcp_reaper
):
    control = tmp_path / "s.json"
    set_behavior(control, tools=["echo"])
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        listed = await _rpc(proc, 1, "tools/list")
        unknown = await _rpc(
            proc, 2, "tools/call", {"name": "nap", "arguments": {}}
        )
        crashed = await _rpc(
            proc, 3, "tools/call", {"name": "crash", "arguments": {}}
        )
        code = await asyncio.wait_for(proc.wait(), _TIMEOUT)

    assert _names(listed["result"]) == ["echo"]
    assert "error" in unknown
    assert "result" not in unknown
    assert crashed is None
    assert code == 3


async def test_echo_reports_trace_from_environment(tmp_path, mcp_reaper):
    control = tmp_path / "s.json"
    cfg = stdio_cfg(control)
    cfg["env"]["TRACE_ID"] = "trace-1"
    async with _raw(cfg) as proc:
        await _initialize(proc)
        reply = await _rpc(
            proc, 1, "tools/call", {"name": "echo", "arguments": {"text": "t"}}
        )

    assert _tool_out(reply) == {
        "text": "t",
        "pid": proc.pid,
        "trace": "trace-1",
    }


# --- потоковый сервер -----------------------------------------------------


async def test_stream_server_on_given_port(tmp_path, mcp_owner, mcp_reaper):
    port = free_port()
    async with stream_server(tmp_path, "stream_http", port=port) as (
        cfg,
        control,
    ):
        out = await call_tool("st", cfg, "action", {})

    assert f":{port}" in cfg["url"]
    assert set(out) == {"count", "session", "pid"}
    assert launches(control) == [out["pid"]]


@pytest.mark.parametrize("transport", ["stream_http", "stream_sse"])
async def test_stream_server_logs_every_request_with_session(
    tmp_path, mcp_owner, mcp_reaper, transport
):
    async with stream_server(tmp_path, transport) as (cfg, control):
        out = await call_tool("st", cfg, "action", {})
        await AgentRuntimeClient("st", cfg).alist_tools(timeout=10)

    rows = [line.split() for line in events(control)]
    requests = [row for row in rows if row[0] == "req"]
    call_sessions = {row[3] for row in requests if row[1] == "tools/call"}
    methods = {row[1] for row in requests}

    assert {"initialize", "tools/list", "tools/call"} <= methods
    assert call_sessions == {str(out["session"])}
    assert all(len(row) == 4 for row in requests)
    opened = [row[3] for row in requests if row[1] == "initialize"]
    # Номер у каждой новой сессии свой: не адрес объекта, который
    # достаётся следующей сессии после сборки прошлой.
    assert len(set(opened)) == len(opened) >= 2


# --- помощники и фикстуры -------------------------------------------------


async def test_fake_clock_sleep_advances():
    clock = FakeClock()

    await clock.sleep(30)
    clock.advance(5)

    assert clock.now() == 35


async def test_wait_for_times_out():
    with pytest.raises(AssertionError):
        await wait_for(lambda: False, timeout=0.2)


async def test_call_tool_is_bounded_as_a_whole(
    tmp_path, mcp_reaper, mcp_owner
):
    """Потолок ``call_tool`` держит и запуск: сервер молчит на initialize.

    Постоянная сессия ждёт ответ ``initialize`` весь срок чтения и только
    потом зовёт тул, так что вызов целиком длиннее потолка. Без потолка
    всего вызова помощник вернул бы ответ тула позже срока; внешний
    ``wait_for`` шире потолка и держит сам тест.
    """
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=60)
    cfg = {**stdio_cfg(control), "persistent_session": True}

    with pytest.raises(AssertionError, match="echo сервера slow"):
        await asyncio.wait_for(
            call_tool("slow", cfg, "echo", {}, timeout=0.5), _TIMEOUT
        )


def test_stdio_server_without_control_refuses_to_start(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GC_TEST_CONTROL"}
    server = stdio_cfg(tmp_path / "unused.json")["args"][0]

    done = subprocess.run(
        [sys.executable, server],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=_TIMEOUT,
        check=False,
    )

    assert done.returncode == 1
    assert list(tmp_path.iterdir()) == []


def test_alive_is_false_for_a_zombie():
    proc = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + _TIMEOUT
        while alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not alive(proc.pid)
        assert proc.returncode is None  # ещё не пожат: зомби
    finally:
        proc.wait()


def test_mcp_home_points_gc_home_at_the_test(mcp_home, tmp_path):
    assert mcp_home == tmp_path / "gc"
    assert env_vars.env.gc_home == mcp_home


async def test_mcp_owner_is_a_fresh_owner(mcp_owner):
    assert current_owner() == mcp_owner


async def test_reaper_closes_tracked_objects(mcp_reaper):
    closed = []

    class _Async:
        async def aclose(self):
            closed.append("async")

    class _Sync:
        def close(self):
            closed.append("sync")

    mcp_reaper.track(_Async())
    mcp_reaper.track(_Sync())
    await mcp_reaper.reap()

    assert sorted(closed) == ["async", "sync"]


class _HungClose:
    async def aclose(self):
        await asyncio.Event().wait()


class _FailingClose:
    async def aclose(self):
        raise RuntimeError("закрытие сломалось")


async def test_reaper_bounds_a_hung_close_and_still_kills(
    tmp_path, mcp_reaper, monkeypatch
):
    monkeypatch.setattr(mcp_fixtures, "_CLOSE_SEC", 0.2)
    control = tmp_path / "s.json"
    async with _raw(stdio_cfg(control)) as proc:
        await _initialize(proc)
        mcp_reaper.track(_HungClose())
        loop = asyncio.get_running_loop()
        started = loop.time()

        with pytest.raises(AssertionError, match="закрытие дольше"):
            await mcp_reaper.reap()

        assert loop.time() - started < 0.2 + _TIMEOUT
        assert not alive(proc.pid)


async def test_reaper_reports_a_failing_close(mcp_reaper):
    mcp_reaper.track(_FailingClose())

    with pytest.raises(AssertionError, match="закрытие сломалось"):
        await mcp_reaper.reap()


async def test_reaper_reads_only_server_journals(tmp_path, mcp_reaper):
    stranger = subprocess.Popen(["sleep", "300"])
    try:
        agent_log = tmp_path / "gc" / "agent.log"
        agent_log.parent.mkdir()
        agent_log.write_text(
            f"start {stranger.pid}\nstart не-число\n", encoding="utf-8"
        )
        (tmp_path / "s.json.mcp-journal").write_text(
            "start не-число\nchild\n", encoding="utf-8"
        )

        await mcp_reaper.reap()

        assert alive(stranger.pid)
    finally:
        stranger.kill()
        stranger.wait()
