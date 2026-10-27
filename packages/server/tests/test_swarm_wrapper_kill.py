"""Сигнал сервера группе обёртки песочницы доходит до CLI.

Сервер снимает песочницу ``os.killpg`` по pid обёртки (отмена, дедлайн,
лимиты памяти и CPU, остановка): Popen с ``start_new_session`` — pid обёртки
и есть её группа. GNU ``timeout`` без ``--foreground`` уводит себя и CLI в
свою группу, и сигнал доставался одному bash: CLI работал до полного
таймаута обёртки (ревью, раунд 4). На машине без GNU ``timeout`` (macOS)
проверять нечего — тест пропускается.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "gc_server"
    / "swarm"
    / "run-cli-in-sandbox.sh"
)
#: Потолок любого ожидания: зависшая песочница — красный тест, не вечный.
_BOUND_SEC = 10.0


def _gnu_timeout() -> bool:
    exe = shutil.which("timeout")
    if exe is None:
        return False
    probe = subprocess.run(
        [exe, "--version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0 and "GNU" in probe.stdout


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None
    or not hasattr(os, "killpg")
    or not _gnu_timeout(),
    reason="нужны bash и GNU timeout",
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(condition) -> bool:
    deadline = time.monotonic() + _BOUND_SEC
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_server_signal_to_the_wrapper_group_reaches_the_cli(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "cli.pid"
    cli = bindir / "fake-gc-cli"
    cli.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec sleep 60\n")
    cli.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "SWARM_ID": "swarm_g_t1",
        "SWARM_THREADS_DIR": str(tmp_path / "threads"),
        "SWARM_SANDBOX_PATH": str(tmp_path / "sandbox"),
        "SWARM_SANDBOX_TOOL": "none",
        "SWARM_CLI_COMMAND": "fake-gc-cli",
        "SWARM_CLI_VENV_ROOT": "",
        "SWARM_PAYLOAD_JSON": "{}",
        "SWARM_LIMIT_TIMEOUT_SECONDS": "60",
    }
    wrapper = subprocess.Popen(
        ["bash", str(WRAPPER)],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cli_pid = None
    try:
        assert _wait(lambda: pidfile.exists() and pidfile.read_text().strip())
        cli_pid = int(pidfile.read_text())
        os.killpg(wrapper.pid, signal.SIGTERM)  # как сервер
        wrapper.wait(timeout=_BOUND_SEC)
        assert _wait(lambda: not _alive(cli_pid)), "CLI пережил сигнал"
    finally:
        if cli_pid is not None and _alive(cli_pid):
            os.kill(cli_pid, signal.SIGKILL)
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
