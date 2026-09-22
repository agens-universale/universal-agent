"""Тесты process_supervisor: PID-файлы, идемпотентный старт, stop."""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recommended_mcp.manifest import RunSpec
from recommended_mcp.process_supervisor import (
    RunSupervisor,
    _popen_kwargs,
    build_command_argv,
    is_pid_alive,
    read_pid,
    remove_pid,
    stop_command_daemons,
    terminate_pid,
    terminate_process,
    write_pid,
)


def test_build_command_argv() -> None:
    run = RunSpec(
        type="command",
        command="science-mcp",
        args=("--transport", "http"),
    )
    with patch(
        "recommended_mcp.process_supervisor.resolve_mcp_command",
        return_value="science-mcp",
    ):
        assert build_command_argv(run) == ["science-mcp", "--transport", "http"]


def test_pid_file_roundtrip(tmp_path: Path) -> None:
    write_pid("science", 4242, tmp_path)
    assert read_pid("science", tmp_path) == 4242
    remove_pid("science", tmp_path)
    assert read_pid("science", tmp_path) is None


def test_is_pid_alive_current_process() -> None:
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(2**31 - 1) is False


def test_start_if_needed_skips_running_process(tmp_path: Path) -> None:
    write_pid("science", os.getpid(), tmp_path)
    supervisor = RunSupervisor(pids_dir=tmp_path)
    run = RunSpec(type="command", command="science-mcp", args=("--transport", "http"))
    with patch("subprocess.Popen") as popen:
        started = supervisor.start_if_needed("science", run)
    assert started is False
    popen.assert_not_called()


def test_start_if_needed_starts_when_no_pid(tmp_path: Path) -> None:
    supervisor = RunSupervisor(pids_dir=tmp_path)
    run = RunSpec(type="command", command="science-mcp", args=("--transport", "http"))
    proc = MagicMock()
    proc.pid = 5555
    proc.poll.return_value = None
    with patch("subprocess.Popen", return_value=proc) as popen:
        started = supervisor.start_if_needed("science", run)
    assert started is True
    popen.assert_called_once()
    assert read_pid("science", tmp_path) == 5555


def test_stop_command_daemons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_pid("science", os.getpid(), tmp_path)
    terminated: list[int] = []
    monkeypatch.setattr(
        "recommended_mcp.process_supervisor.terminate_pid",
        lambda pid, **kwargs: terminated.append(pid),
    )
    stopped = stop_command_daemons(["science"], pids_dir=tmp_path)
    assert stopped == 1
    assert terminated == [os.getpid()]
    assert read_pid("science", tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows гашение через taskkill, зомби нет")
def test_terminate_process_returns_fast_for_own_child() -> None:
    """Умерший по SIGTERM собственный ребёнок остаётся зомби, и `kill(pid, 0)`
    видел его живым: гашение ждало весь graceful-таймаут (5 с) на каждый daemon."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        **_popen_kwargs(None, None),
    )
    time.sleep(0.3)
    started = time.monotonic()
    terminate_process(proc)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert is_pid_alive(proc.pid) is False


# Ребёнок в группе лидера, глухой к SIGTERM (как Chromium под MCP-обёрткой).
# Pid пишет сам ребёнок и уже после SIG_IGN: иначе SIGTERM мог бы опередить
# установку обработчика, и тест проходил бы случайно.
_STUBBORN_CHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(60)\n"
)


def _spawn_leader_with_stubborn_child(pid_file: Path, *, leader_stays: bool) -> subprocess.Popen:
    """Лидер своей группы запускает глухого к SIGTERM ребёнка в ту же группу;
    дальше лидер либо спит, либо сразу выходит (дождавшись pid ребёнка)."""
    leader = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {_STUBBORN_CHILD!r}, {str(pid_file)!r}])\n"
        f"while not os.path.exists({str(pid_file)!r}):\n"
        "    time.sleep(0.05)\n"
        + ("time.sleep(60)\n" if leader_stays else "")
    )
    return subprocess.Popen([sys.executable, "-c", leader], **_popen_kwargs(None, None))


def _read_child_pid(pid_file: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while not pid_file.is_file():
        if time.monotonic() >= deadline:
            pytest.fail("ребёнок не записал pid")
        time.sleep(0.05)
    return int(pid_file.read_text(encoding="utf-8"))


def _alive_after(pid: int, timeout: float = 2.0) -> bool:
    """Жив ли процесс, дав init'у время пожать убитую сироту."""
    deadline = time.monotonic() + timeout
    while is_pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return is_pid_alive(pid)


def _kill_leftovers(proc: subprocess.Popen, child: int | None) -> None:
    for kill, target in ((os.killpg, proc.pid), (os.kill, child)):
        if target is None:
            continue
        try:
            kill(target, signal.SIGKILL)
        except OSError:
            pass
    proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="группы процессов — только POSIX")
def test_terminate_process_kills_child_ignoring_sigterm(tmp_path: Path) -> None:
    """Лидер умер от SIGTERM, а ребёнок в его группе SIGTERM игнорирует: ждать надо
    группу, а не лидера, иначе SIGKILL группе не уходит и ребёнок переживает гашение."""
    pid_file = tmp_path / "child.pid"
    proc = _spawn_leader_with_stubborn_child(pid_file, leader_stays=True)
    child = None
    try:
        child = _read_child_pid(pid_file)
        terminate_process(proc, graceful_timeout=1.0)
        assert _alive_after(child) is False
    finally:
        _kill_leftovers(proc, child)


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="нужен waitid с WNOWAIT")
def test_terminate_process_kills_group_of_dead_leader(tmp_path: Path) -> None:
    """Лидер уже вышел (зомби, ещё не забран), его ребёнок в группе жив: мёртвый
    лидер — не повод пропустить гашение, группа гасится тем же порядком."""
    pid_file = tmp_path / "child.pid"
    proc = _spawn_leader_with_stubborn_child(pid_file, leader_stays=False)
    child = None
    try:
        child = _read_child_pid(pid_file)
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)   # дождаться, не забирая
        terminate_process(proc, graceful_timeout=1.0)
        assert _alive_after(child) is False
    finally:
        _kill_leftovers(proc, child)


@pytest.mark.skipif(sys.platform == "win32", reason="группы процессов — только POSIX")
def test_terminate_process_leaves_group_of_reaped_leader_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Лидера уже забрали: его номер свободен и мог достаться чужой группе —
    сигнал по нему погасил бы её."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **_popen_kwargs(None, None))
    proc.wait(timeout=10)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))

    terminate_process(proc)

    assert signalled == []


@pytest.mark.skipif(sys.platform == "win32", reason="группы процессов — только POSIX")
@pytest.mark.parametrize(
    "terminate",
    [terminate_process, lambda proc, **kw: terminate_pid(proc.pid, **kw)],
    ids=["process", "pid"],
)
def test_orphans_of_exited_leader_do_not_hold_full_timeout(tmp_path: Path, terminate) -> None:
    """Лидер вышел по SIGTERM, глухой ребёнок остался: ждать его весь graceful-таймаут
    незачем. Демоны гасятся по очереди, а аварийный выход ограничен общим бюджетом —
    два таких демона съели бы его целиком."""
    pid_file = tmp_path / "child.pid"
    proc = _spawn_leader_with_stubborn_child(pid_file, leader_stays=True)
    child = None
    try:
        child = _read_child_pid(pid_file)
        started = time.monotonic()
        terminate(proc, graceful_timeout=10.0)
        elapsed = time.monotonic() - started
        assert elapsed < 3.0
        assert _alive_after(child) is False
    finally:
        _kill_leftovers(proc, child)


def test_shutdown_managed_survives_broken_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Печать в мёртвый терминал бросает OSError (EIO): она не должна обрывать
    гашение остальных демонов."""
    supervisor = RunSupervisor(pids_dir=tmp_path)
    procs = []
    for name, pid in (("first", 1111), ("second", 2222)):
        proc = MagicMock()
        proc.pid = pid
        write_pid(name, pid, tmp_path)
        supervisor._managed.append((name, proc))
        procs.append(proc)
    terminated: list[object] = []
    monkeypatch.setattr(
        "recommended_mcp.process_supervisor.terminate_process",
        lambda proc, **kwargs: terminated.append(proc),
    )

    def _broken_print(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr("builtins.print", _broken_print)
    supervisor.shutdown_managed()
    monkeypatch.undo()

    assert terminated == [procs[1], procs[0]]
    assert supervisor._managed == []
    assert read_pid("first", tmp_path) is None
    assert read_pid("second", tmp_path) is None
