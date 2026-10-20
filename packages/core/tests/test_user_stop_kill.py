"""Стоп хода снимает то, чего ход ждёт, и пишет в лог только это.

Критерии — docs_md/actual/2026-10-14-user-stop-kill-SPEC.ru.md, §6.
Таймауты конфига здесь не меняются: без стопа команда по-прежнему
досиживает свой потолок.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from types import SimpleNamespace

import pytest
from gc_core.backends.async_protocol import SyncExecAdapter
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult
from gc_core.backends.remote_shell import (
    RemoteShellBackend,
    watchdog_wrap,
)
from gc_core.backends.sandbox import SandboxBackend
from gc_core.execution.contracts import ModelRequestStopped
from gc_core.tools.connect_mcp.client_port import _mcp_call_under_stop
from gc_core.tools.connect_mcp.transport import _LineReader
from gc_core.user_stop import (
    INTERRUPTED_CODE,
    UserStop,
    UserStopInterrupt,
    bind_tool_call,
    bind_user_stop,
)
from gc_core.utils.connect_backend import (
    _astream_chat_completions,
    _default_should_retry_backend_exception,
)
from gc_core.utils.native_fs_search import arun_native_grep
from gc_core.utils.pattern_native_gate import NativeGrepPlan


class _Log:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, msg, *args, extra=None) -> None:
        del msg, args
        self.records.append(dict(extra or {}))

    def events(self, name: str) -> list[dict]:
        return [r for r in self.records if r.get("event") == name]


def _stop(log: _Log | None = None) -> UserStop:
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), log)
    return gate


async def _kill_during(gate: UserStop, log: _Log, work, *, delay: float = 0.3):
    task = asyncio.create_task(work)
    await asyncio.sleep(delay)
    gate.request(log)
    return await task


def test_stop_is_not_a_retry():
    assert (
        _default_should_retry_backend_exception(
            ModelRequestStopped("Ход остановлен")
        )
        is False
    )
    assert (
        _default_should_retry_backend_exception(
            UserStopInterrupt(kind="model")
        )
        is False
    )


def test_watchdog_without_stop_file_is_unchanged():
    body = "cd '/w' || exit; echo hi"
    assert watchdog_wrap(body, 5, "timeout") == watchdog_wrap(
        body, 5, "timeout", stop_file=None
    )
    wrapped = watchdog_wrap(
        body, 5, "timeout", stop_file="/tmp/gc-user-stop-x"
    )
    assert wrapped.startswith("echo $$ > /tmp/gc-user-stop-x; exec timeout ")


async def test_nothing_in_flight_is_not_logged():
    log = _Log()
    gate = _stop(log)
    gate.request(log)
    assert log.records == []


async def test_model_stream_aborts_without_a_partial_reply():
    log = _Log()
    gate = _stop(log)
    seen: list[str] = []
    closed = asyncio.Event()

    class _Response:
        def raise_for_status(self) -> None:
            return None

        async def aclose(self) -> None:
            closed.set()

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await closed.wait()
            raise ConnectionError("closed")

    class _Client:
        def __init__(self) -> None:
            self.response = _Response()

        def stream(self, *args, **kwargs):
            del args, kwargs
            return self

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *args):
            return None

    def _on_delta(kind: str, text: str) -> None:
        del kind
        if text:
            seen.append(text)

    async def _run():
        with bind_user_stop(gate):
            return await _astream_chat_completions(
                _Client(),
                "http://model/v1/chat/completions",
                {"model": "m", "messages": []},
                {},
                timeout=600,
                logger=None,
                on_delta=_on_delta,
            )

    task = asyncio.create_task(_run())
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.02)
    assert seen == ["hi"]
    started = time.monotonic()
    gate.request(log)
    with pytest.raises(ModelRequestStopped):
        await task
    assert time.monotonic() - started < 3
    assert closed.is_set()
    killed = log.events("user_stop_killed")
    assert len(killed) == 1
    assert killed[0]["kind"] == "model"
    assert killed[0]["tool"] == ""
    assert killed[0]["backend"] == ""
    assert "hi" not in str(killed[0])


async def _stopped_exec(backend, command: str, log: _Log, **kwargs):
    gate = _stop(log)

    async def _work():
        with bind_user_stop(gate):
            return await backend.aexec(command, timeout=30, **kwargs)

    started = time.monotonic()
    result = await _kill_during(gate, log, _work())
    return result, time.monotonic() - started


async def test_local_exec_is_killed_and_keeps_printed_output(tmp_path):
    del tmp_path
    log = _Log()
    result, elapsed = await _stopped_exec(
        LocalFsBackend(), "echo HI; sleep 30", log
    )
    assert elapsed < 8
    assert result.stopped_by_user is True
    assert result.timed_out is False
    assert result.return_code != 0
    assert "HI" in result.stdout
    killed = log.events("user_stop_killed")
    assert len(killed) == 1
    assert killed[0]["kind"] == "exec"
    assert killed[0]["backend"] == "LocalFsBackend"
    assert "HI" not in str(killed[0])
    assert log.events("user_stop_unkillable") == []


async def test_sandbox_exec_is_killed_the_same_way(tmp_path):
    log = _Log()
    backend = SandboxBackend(wrapper=lambda cwd: [], root=str(tmp_path))
    result, elapsed = await _stopped_exec(backend, "echo HI; sleep 30", log)
    assert elapsed < 8
    assert result.stopped_by_user is True
    assert "HI" in result.stdout
    assert log.events("user_stop_killed")[0]["backend"] == "SandboxBackend"


async def test_remote_shell_command_does_not_sit_out_its_timeout():
    log = _Log()
    backend = RemoteShellBackend(
        ["env"],
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=1000,
        walk_output_max_chars=1000,
    )
    result, elapsed = await _stopped_exec(backend, "echo HI; sleep 30", log)
    assert elapsed < 8
    assert result.stopped_by_user is True
    assert result.timed_out is False
    assert "HI" in result.stdout
    killed = log.events("user_stop_killed")
    assert len(killed) == 1
    assert killed[0]["backend"] == "RemoteShellBackend"
    assert "sleep" not in str(killed[0])


async def test_remote_without_watchdog_aborts_and_logs_unkillable(monkeypatch):
    log = _Log()
    backend = RemoteShellBackend(
        ["env"],
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=1000,
        walk_output_max_chars=1000,
    )

    async def _no_tool(self):
        del self
        return None

    monkeypatch.setattr(RemoteShellBackend, "_awatchdog", _no_tool)
    result, elapsed = await _stopped_exec(backend, "echo HI; sleep 30", log)
    assert elapsed < 8
    assert result.stopped_by_user is True
    unknown = log.events("user_stop_unkillable")
    assert len(unknown) == 1
    assert unknown[0]["backend"] == "RemoteShellBackend"
    assert "HI" not in str(unknown[0])
    assert log.events("user_stop_killed") == []


async def test_harbor_exec_is_not_cut_by_stop():
    """Стоп посреди тула Harbor команду не переписывает и не обрывает.

    Внешняя отмена тула здесь есть, как у ``_acall_under_stop``. Жест забирает
    сам ``env.exec`` и держит его, пока команда не выйдет сама.
    """
    from gc_core.backends.harbor import HarborBackend
    from gc_testkit.runtime_probe import probe_total_in_command

    class _Res:
        def __init__(self, stdout: str, return_code: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.return_code = return_code

    class _Env:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.finish = asyncio.Event()
            self.commands: list[str] = []
            self.timeouts: list[object] = []

        async def exec(self, command, **kwargs):
            total = probe_total_in_command(str(command))
            if total is not None:
                return _Res(f"{total}\n")
            self.commands.append(str(command))
            self.timeouts.append(kwargs.get("timeout_sec"))
            self.started.set()
            await self.finish.wait()
            return _Res("HI\n", return_code=0)

    log = _Log()
    gate = _stop(log)
    env = _Env()
    backend = HarborBackend(env, asyncio.get_running_loop(), exec_init=None)

    async def _body():
        return await backend.aexec("echo HI; sleep 30", timeout=30)

    async def _work():
        with bind_user_stop(gate):
            task = asyncio.create_task(_body())

            async def _release() -> None:
                if not task.done():
                    task.cancel()

            async with gate.arm(
                kind="tool",
                tool="exec_command",
                call_id="c1",
                release=_release,
            ):
                return await task

    task = asyncio.create_task(_work())
    await asyncio.wait_for(env.started.wait(), timeout=2)
    gate.request(log)
    await asyncio.sleep(0.05)
    assert not task.done()
    assert env.commands == ["echo HI; sleep 30"]
    assert "setsid" not in env.commands[0]
    env.finish.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.stopped_by_user is False
    assert result.timed_out is False
    assert result.return_code == 0
    assert "HI" in result.stdout
    assert env.timeouts == [30]
    assert log.events("user_stop_killed") == []
    assert log.events("user_stop_unkillable") == []


async def test_foreign_sync_without_kill_aborts_the_wait():
    log = _Log()
    gate = _stop(log)
    started = threading.Event()

    class _Foreign:
        def exec(self, command, **kwargs):
            del command, kwargs
            started.set()
            time.sleep(30)
            return ExecResult(return_code=0, stdout="late")

    async def _work():
        with bind_user_stop(gate):
            return await SyncExecAdapter(_Foreign()).aexec(
                "sleep 30", timeout=30
            )

    task = asyncio.create_task(_work())
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    t0 = time.monotonic()
    gate.request(log)
    result = await task
    assert time.monotonic() - t0 < 3
    assert result.stopped_by_user is True
    unknown = log.events("user_stop_unkillable")
    assert len(unknown) == 1
    assert unknown[0]["backend"] == "_Foreign"
    assert "sleep" not in str(unknown[0])


async def test_foreign_sync_with_stop_is_killed():
    log = _Log()
    gate = _stop(log)
    started = threading.Event()

    class _Foreign:
        def __init__(self) -> None:
            self.go = threading.Event()
            self.stopped = False

        def exec(self, command, **kwargs):
            del command, kwargs
            started.set()
            self.go.wait(30)
            return ExecResult(return_code=0, stdout="x")

        def stop_exec(self) -> None:
            self.stopped = True
            self.go.set()

    foreign = _Foreign()

    async def _work():
        with bind_user_stop(gate):
            return await SyncExecAdapter(foreign).aexec("sleep 30", timeout=30)

    task = asyncio.create_task(_work())
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    gate.request(log)
    result = await task
    assert foreign.stopped is True
    assert result.stopped_by_user is True
    assert log.events("user_stop_killed")[0]["kind"] == "exec"
    assert log.events("user_stop_unkillable") == []


async def test_foreign_sync_already_stopped_does_not_run():
    """Стоп уже взведён: чужой exec не стартует и не досиживает sleep."""
    log = _Log()
    gate = _stop(log)
    gate.request(log)
    entered = threading.Event()
    finished = threading.Event()

    class _Foreign:
        def exec(self, command, **kwargs):
            del command, kwargs
            entered.set()
            time.sleep(0.3)
            finished.set()
            return ExecResult(return_code=0, stdout="late")

    t0 = time.monotonic()
    with bind_user_stop(gate):
        result = await SyncExecAdapter(_Foreign()).aexec(
            "sleep 30", timeout=30
        )
    assert time.monotonic() - t0 < 1
    assert result.stopped_by_user is True
    assert result.stdout == ""
    assert result.return_code == INTERRUPTED_CODE
    await asyncio.sleep(0.05)
    assert not entered.is_set()
    await asyncio.sleep(0.45)
    assert not finished.is_set()
    assert log.records == []


async def test_tool_already_stopped_does_not_start():
    from gc_core.tools import _acall_under_stop

    log = _Log()
    gate = _stop(log)
    gate.request(log)
    ran = asyncio.Event()

    async def _body():
        ran.set()
        await asyncio.sleep(30)
        return "no"

    with bind_user_stop(gate):
        with pytest.raises(UserStopInterrupt) as caught:
            await _acall_under_stop("search", "c1", _body, None)
    assert caught.value.started is False
    await asyncio.sleep(0.05)
    assert not ran.is_set()
    assert log.records == []


async def test_mcp_already_stopped_does_not_start():
    log = _Log()
    gate = _stop(log)
    gate.request(log)
    ran = asyncio.Event()

    async def _body():
        ran.set()
        await asyncio.sleep(30)
        return {"ok": True}

    with bind_user_stop(gate):
        with pytest.raises(UserStopInterrupt) as caught:
            await _mcp_call_under_stop(
                SimpleNamespace(server_name="docs"), "lookup", _body
            )
    assert caught.value.started is False
    assert caught.value.kind == "mcp"
    await asyncio.sleep(0.05)
    assert not ran.is_set()
    assert log.records == []


async def test_model_post_already_stopped_does_not_send():
    from gc_core.utils.connect_backend import _aguard_model_call

    log = _Log()
    gate = _stop(log)
    gate.request(log)
    sent = asyncio.Event()

    async def _post():
        sent.set()
        await asyncio.sleep(30)
        return "ok"

    with bind_user_stop(gate):
        with pytest.raises(ModelRequestStopped):
            await _aguard_model_call(_post)
    await asyncio.sleep(0.05)
    assert not sent.is_set()
    assert log.records == []


async def test_native_grep_does_not_sit_out_its_budget(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    grep = bindir / "grep"
    grep.write_text("#!/bin/sh\necho HIT\nsleep 30\n", encoding="utf-8")
    grep.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    target = tmp_path / "dir"
    target.mkdir()
    (target / "a.txt").write_text("x\n", encoding="utf-8")
    log = _Log()
    gate = _stop(log)

    async def _work():
        with bind_user_stop(gate):
            return await arun_native_grep(
                LocalFsBackend(),
                str(target),
                NativeGrepPlan(
                    fixed=True, pattern="x", case_insensitive=False
                ),
                exclude_dir_names=(),
                exclude_nested_dot_dirs=False,
                file_pattern=None,
                max_matches=None,
                report_path=lambda path: path,
                is_dir_target=True,
            )

    task = asyncio.create_task(_work())
    await asyncio.sleep(0.4)
    t0 = time.monotonic()
    gate.request(log)
    with pytest.raises(UserStopInterrupt):
        await task
    assert time.monotonic() - t0 < 8
    assert log.events("user_stop_killed")[0]["kind"] == "exec"


async def test_mcp_call_returns_and_the_server_process_stays():
    log = _Log()
    gate = _stop(log)
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "60",
        start_new_session=True,
    )
    try:
        client = SimpleNamespace(server_name="docs")

        async def _hang():
            await asyncio.sleep(30)
            return {"ok": True}

        async def _work():
            with bind_user_stop(gate):
                with bind_tool_call("lookup", "call-1"):
                    return await _mcp_call_under_stop(client, "lookup", _hang)

        task = asyncio.create_task(_work())
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        gate.request(log)
        with pytest.raises(UserStopInterrupt):
            await task
        assert time.monotonic() - t0 < 3
        assert proc.returncode is None
        killed = log.events("user_stop_killed")
        assert len(killed) == 1
        assert killed[0]["kind"] == "mcp"
        assert killed[0]["tool"] == "lookup"
        assert killed[0]["call_id"] == "call-1"
        assert killed[0]["mcp_server"] == "docs"
        assert killed[0]["backend"] == ""
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_stdio_readline_notices_stop_without_closing_the_process():
    log = _Log()
    gate = _stop(log)
    proc = await asyncio.create_subprocess_exec("sleep", "60")
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    reader = _LineReader(stream)
    try:

        async def _read():
            with bind_user_stop(gate):
                return await asyncio.to_thread(reader.readline, 120)

        task = asyncio.create_task(_read())
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        gate.request(log)
        with pytest.raises(UserStopInterrupt):
            await task
        assert time.monotonic() - t0 < 3
        assert proc.returncode is None
    finally:
        os.close(write_fd)
        stream.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_tool_await_is_cancelled_sync_step_finishes():
    from gc_core.tools import _acall_under_stop

    log = _Log()
    gate = _stop(log)

    async def _hung():
        await asyncio.sleep(30)
        return "no"

    async def _work():
        with bind_user_stop(gate):
            return await _acall_under_stop("search", "c1", _hung, None)

    task = asyncio.create_task(_work())
    await asyncio.sleep(0.05)
    gate.request(log)
    with pytest.raises(UserStopInterrupt) as caught:
        await task
    assert caught.value.started is True
    assert log.events("user_stop_killed")[0]["kind"] == "tool"
    assert log.events("user_stop_killed")[0]["tool"] == "search"

    gate2 = _stop()

    async def _read():
        time.sleep(0.15)
        return "bytes"

    async def _finish():
        with bind_user_stop(gate2):
            return await _acall_under_stop("read", "c2", _read, None)

    task2 = asyncio.create_task(_finish())
    await asyncio.sleep(0.02)
    gate2.request(_Log())
    assert await task2 == "bytes"


async def test_background_pid_survives_a_later_stop():
    log = _Log()
    gate = _stop(log)
    backend = LocalFsBackend()
    spawned = await backend.aspawn_background("sleep 30")
    pid = int(spawned["pid"])
    try:
        os.kill(pid, 0)

        async def _work():
            with bind_user_stop(gate):
                return await backend.aexec("echo HI; sleep 30", timeout=30)

        result = await _kill_during(gate, log, _work())
        assert result.stopped_by_user is True
        os.kill(pid, 0)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_subagent_command_is_the_parents_stop():
    log = _Log()
    gate = _stop(log)
    backend = LocalFsBackend()

    async def _child():
        return await backend.aexec("echo HI; sleep 30", timeout=30)

    async def _parent():
        with bind_user_stop(gate):
            child = asyncio.create_task(_child())
            await asyncio.sleep(0.3)
            gate.request(log)
            return await child

    started = time.monotonic()
    result = await _parent()
    assert time.monotonic() - started < 8
    assert result.stopped_by_user is True
    assert "HI" in result.stdout
    assert len(log.events("user_stop_killed")) == 1


async def test_timeout_still_kills_when_nobody_stopped():
    started = time.monotonic()
    result = await LocalFsBackend().aexec("sleep 30", timeout=1)
    assert time.monotonic() - started < 5
    assert result.timed_out is True
    assert result.stopped_by_user is False


async def test_server_stop_returns_when_the_command_is_dead():
    from gc_server.rest_api.agent_execution_registry import (
        ActiveRun,
        AgentRunRegistry,
    )

    log = _Log()
    gate = _stop(log)
    dead = asyncio.Event()

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            gate.request(log)

    registry = AgentRunRegistry()
    registry._runs["sys:task"] = ActiveRun(
        key="sys:task",
        system="sys",
        task_id="task",
        executor=_Executor(),
        thread=None,
        started_at=time.time(),
        finished_at=None,
        thread_id=None,
        last_activity_at=time.time(),
    )

    async def _work():
        with bind_user_stop(gate):
            result = await LocalFsBackend().aexec("sleep 30", timeout=30)
            dead.set()
            return result

    task = asyncio.create_task(_work())
    await asyncio.sleep(0.3)
    returned = threading.Event()

    def _call() -> None:
        assert registry.stop(system="sys", task_id="task") is True
        returned.set()

    worker = threading.Thread(target=_call)
    worker.start()
    result = await task
    worker.join(5)
    assert returned.is_set()
    assert dead.is_set()
    assert result.stopped_by_user is True
    assert log.events("user_stop_killed")


def test_tui_stop_waits_off_the_loop():
    from gc_tui.services.agent_runner import AgentRunner

    log = _Log()
    loop = asyncio.new_event_loop()
    gate = UserStop()

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            gate.request(log)

    async def _arm() -> None:
        gate.prepare_run(asyncio.get_running_loop(), log)
        with bind_user_stop(gate):
            await LocalFsBackend().aexec("sleep 30", timeout=30)

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_arm())

    thread = threading.Thread(target=_run)
    thread.start()
    for _ in range(50):
        if gate._stack:
            break
        time.sleep(0.02)
    runner = AgentRunner(_Executor())
    t0 = time.monotonic()
    runner.stop()
    assert time.monotonic() - t0 < 8
    thread.join(5)
    assert not thread.is_alive()
    assert log.events("user_stop_killed")
    loop.close()


def test_cli_sigint_stops_the_run_and_ask_human_stays_a_refusal():
    from gc_cli.run_stop import install_run_sigint, restore_run_sigint
    from gc_core.utils.sigint_waits import intercept_sigint

    calls = {"stop": 0}

    class _Executor:
        def stop_run(self) -> None:
            calls["stop"] += 1

    previous = install_run_sigint(_Executor())
    try:
        refused = {"n": 0}
        with intercept_sigint(lambda: refused.__setitem__("n", 1)):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert calls["stop"] == 0
        assert refused["n"] == 1
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert calls["stop"] == 1
        with pytest.raises(KeyboardInterrupt):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
    finally:
        restore_run_sigint(previous)


async def test_chat_and_acp_stop_wait_until_settled():
    from gc_acp.facade import _await_live_stop
    from gc_core.user_stop import await_agent_stop

    log = _Log()
    gate = _stop(log)
    released = asyncio.Event()

    class _Agent:
        def __init__(self) -> None:
            self._executor = SimpleNamespace(user_stop=gate)

        def stop(self) -> None:
            gate.request(log)

        async def astop(self) -> None:
            self.stop()
            await gate.await_settled()

    async def _arm() -> None:
        async def _release() -> None:
            released.set()

        with bind_user_stop(gate):
            async with gate.arm(kind="model", release=_release):
                await released.wait()

    task = asyncio.create_task(_arm())
    await asyncio.sleep(0.05)
    await await_agent_stop(_Agent())
    assert released.is_set()
    await task

    gate2 = _stop()
    held = asyncio.Event()

    class _Live:
        agent = SimpleNamespace(_executor=SimpleNamespace(user_stop=gate2))

    async def _hold() -> None:
        async def _release() -> None:
            held.set()

        with bind_user_stop(gate2):
            async with gate2.arm(kind="exec", release=_release):
                await held.wait()

    held_task = asyncio.create_task(_hold())
    await asyncio.sleep(0.05)

    async def _cancel() -> None:
        gate2.request(log)
        await _await_live_stop(_Live())

    await _cancel()
    assert held.is_set()
    await held_task


def test_same_loop_registry_stop_does_not_block():
    """Стоп с потока loop не ждёт: иначе реестр встал бы на собственном цикле."""
    from gc_server.rest_api.agent_execution_registry import (
        ActiveRun,
        AgentRunRegistry,
    )

    loop = asyncio.new_event_loop()
    gate = UserStop()
    gate._loop = loop
    gate._settled.clear()

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            return None

    registry = AgentRunRegistry()
    registry._runs["sys:task"] = ActiveRun(
        key="sys:task",
        system="sys",
        task_id="task",
        executor=_Executor(),
        thread=None,
        started_at=time.time(),
        finished_at=None,
        thread_id=None,
        last_activity_at=time.time(),
    )

    def _on_loop() -> None:
        started = time.monotonic()
        assert registry.stop(system="sys", task_id="task") is True
        assert time.monotonic() - started < 1

    try:
        loop.call_soon_threadsafe(_on_loop)
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()


def test_run_logger_accepts_the_stop_event():
    """Запись идёт в лог прогона: extra.event, без вывода команды."""
    log = logging.getLogger("gc-test-user-stop")
    log.setLevel(logging.INFO)
    found: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            found.append(record)

    handler = _Handler()
    log.addHandler(handler)
    try:
        loop = asyncio.new_event_loop()
        gate = UserStop()
        gate.prepare_run(loop, log)

        done = asyncio.Event()

        async def _arm() -> None:
            async def _release() -> None:
                done.set()

            async with gate.arm(
                kind="exec",
                tool="exec_command",
                call_id="c",
                backend="LocalFsBackend",
                release=_release,
            ):
                await done.wait()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_arm())

        thread = threading.Thread(target=_run)
        thread.start()
        for _ in range(50):
            if gate._stack:
                break
            time.sleep(0.02)
        gate.request(log)
        thread.join(5)
        loop.close()
    finally:
        log.removeHandler(handler)
    assert len(found) == 1
    assert found[0].event == "user_stop_killed"  # type: ignore[attr-defined]
    assert found[0].kind == "exec"  # type: ignore[attr-defined]
    assert not hasattr(found[0], "stdout")
