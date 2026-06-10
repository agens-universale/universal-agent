"""Тесты process_supervisor: PID-файлы, идемпотентный старт, stop."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recommended_mcp.manifest import RunSpec
from recommended_mcp.process_supervisor import (
    RunSupervisor,
    build_command_argv,
    is_pid_alive,
    read_pid,
    remove_pid,
    stop_command_daemons,
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
