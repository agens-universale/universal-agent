"""Кроссплатформенный супервизор daemon-команд для recommended_mcp/run.py."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from gc_core.mcp_runtime.env import augment_process_env, resolve_mcp_command
from recommended_mcp.manifest import RunSpec


class SupervisorError(RuntimeError):
    """Невозможно запустить или остановить supervised-процесс."""


def default_pids_dir() -> Path:
    from gc_core.mcp_runtime.paths import recommended_mcp_data_dir

    path = recommended_mcp_data_dir() / "pids"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_file_path(name: str, pids_dir: Path | None = None) -> Path:
    root = pids_dir or default_pids_dir()
    return root / f"{name}.pid"


def read_pid(name: str, pids_dir: Path | None = None) -> int | None:
    path = pid_file_path(name, pids_dir)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def write_pid(name: str, pid: int, pids_dir: Path | None = None) -> None:
    path = pid_file_path(name, pids_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid(name: str, pids_dir: Path | None = None) -> None:
    path = pid_file_path(name, pids_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def build_command_argv(run: RunSpec) -> list[str]:
    if not run.command:
        raise SupervisorError("run.type=command requires run.command")
    return [resolve_mcp_command(run.command), *[str(arg) for arg in run.args]]


def _popen_kwargs(env: dict[str, str] | None, cwd: Path | None) -> dict:
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "cwd": str(cwd) if cwd else None,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return kwargs


def terminate_pid(pid: int, *, graceful_timeout: float = 5.0) -> None:
    """Остановить процесс и его группу/дерево по PID."""
    if not is_pid_alive(pid):
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                check=False,
                capture_output=True,
            )
    except ProcessLookupError:
        return
    except Exception:
        try:
            if os.name == "posix":
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    deadline = time.monotonic() + graceful_timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return
        time.sleep(0.1)

    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
    except Exception:
        try:
            if os.name == "posix":
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def terminate_process(proc: subprocess.Popen, *, graceful_timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    terminate_pid(proc.pid, graceful_timeout=graceful_timeout)


class RunSupervisor:
    """Запускает daemon-команды, следит за PID-файлами, гасит детей при выходе."""

    def __init__(self, pids_dir: Path | None = None) -> None:
        self._pids_dir_override = pids_dir
        self._managed: list[tuple[str, subprocess.Popen]] = []

    @property
    def pids_dir(self) -> Path:
        if self._pids_dir_override is not None:
            return self._pids_dir_override
        return default_pids_dir()

    def start_if_needed(
        self,
        name: str,
        run: RunSpec,
        *,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bool:
        """Запустить команду, если ещё не жива. True — стартовали сейчас."""
        existing = read_pid(name, self.pids_dir)
        if existing is not None:
            if is_pid_alive(existing):
                print(f"[skip] {name}: already running (pid {existing})")
                return False
            remove_pid(name, self.pids_dir)

        cmd = build_command_argv(run)
        env = augment_process_env()
        for key, value in run.env:
            env[str(key)] = str(value)
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})

        print(f"[run] {name}: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, **_popen_kwargs(env, cwd or run.cwd))
        except OSError as exc:
            raise SupervisorError(f"mcp {name!r}: failed to start {cmd!r}: {exc}") from exc

        write_pid(name, proc.pid, self.pids_dir)
        self._managed.append((name, proc))
        return True

    def shutdown_managed(self) -> None:
        for name, proc in reversed(self._managed):
            print(f"[stop] {name}: pid {proc.pid}")
            terminate_process(proc)
            remove_pid(name, self.pids_dir)
        self._managed.clear()

    def wait_until_signal(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise SupervisorError("wait_until_signal must run on the main thread")

        stop = threading.Event()

        def _handler(signum: int, frame: object) -> None:
            stop.set()

        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handler)
        try:
            while not stop.is_set():
                for name, proc in list(self._managed):
                    if proc.poll() is not None:
                        code = proc.returncode
                        remove_pid(name, self.pids_dir)
                        self._managed = [(n, p) for n, p in self._managed if n != name]
                        raise SupervisorError(
                            f"mcp {name!r} exited unexpectedly (code {code})"
                        )
                stop.wait(timeout=0.5)
        finally:
            signal.signal(signal.SIGINT, previous_int)
            if hasattr(signal, "SIGTERM") and previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)


def stop_command_daemons(names: list[str], pids_dir: Path | None = None) -> int:
    """Остановить daemon'ы по PID-файлам. Возвращает число остановленных."""
    root = pids_dir or default_pids_dir()
    stopped = 0
    for name in names:
        pid = read_pid(name, root)
        if pid is None:
            print(f"[skip] {name}: not running (no pid file)")
            continue
        if not is_pid_alive(pid):
            print(f"[skip] {name}: stale pid file ({pid})")
            remove_pid(name, root)
            continue
        print(f"[stop] {name}: pid {pid}")
        terminate_pid(pid)
        remove_pid(name, root)
        stopped += 1
    return stopped
