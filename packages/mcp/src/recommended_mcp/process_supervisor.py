"""Кроссплатформенный супервизор daemon-команд для recommended_mcp/run.py."""

from __future__ import annotations

import contextlib
import functools
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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


def _reap_child(pid: int) -> bool:
    """Забрать своего ребёнка-зомби и ответить, вышел ли процесс: `kill(…, 0)`
    видит зомби живым и членом группы, и гашение ждало бы весь graceful-таймаут у
    давно умершего процесса. Не наш ребёнок (`ChildProcessError`) — забирать
    нечего, выход видно по самому pid."""
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return not is_pid_alive(pid)
    return reaped == pid


def _anyone_left(kill: Callable[[int], None]) -> bool:
    """Есть ли кто-нибудь у цели сигнала, живой или не пожатый."""
    try:
        kill(0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                    # кто-то есть, но сигналить ему нам нельзя
    return True


# Сколько ждать тех, кто пережил вышедшего лидера. Лидер — обёртка демона, и
# раз он уже вышел, оставшиеся — то, что сама обёртка не погасила; весь
# graceful-таймаут на них не тратим: аварийный выход TUI ограничен общим бюджетом.
_ORPHAN_GRACE_S = 1.0
# После SIGKILL: дождаться, пока ядро снимет процессы, и забрать своих — иначе
# убитый ребёнок остаётся зомби и числится живым.
_KILL_WAIT_S = 1.0


@dataclass
class _Target:
    """Цель гашения: `kill(sig)` сигналит группе (`os.killpg`), а в откате —
    одному процессу; `leader_gone()` забирает своего лидера-зомби и отвечает,
    вышел ли он."""

    kill: Callable[[int], None]
    leader_gone: Callable[[], bool]
    deadline: float = 0.0
    killed: bool = False


def _terminate_posix(targets: list[_Target], *, graceful_timeout: float) -> None:
    """Погасить группы РАЗОМ: SIGTERM всем → одно общее ожидание до
    graceful_timeout → SIGKILL оставшимся → до `_KILL_WAIT_S` забрать убитых.

    Разом, а не по очереди: по очереди каждый демон, глухой к SIGTERM, стоил весь
    graceful-таймаут, время гашения росло с числом демонов, и аварийный выход TUI
    с общим бюджетом обрывал его — последние демоны оставались жить.

    Ждём ГРУППУ, а не лидера. Лидер — обёртка демона, а тяжёлое у него в детях:
    лидер по SIGTERM выходит сразу, Chromium в той же группе SIGTERM игнорирует.
    Пока ожидание смотрело на лидера, его смерть засчитывалась за смерть всех, и
    SIGKILL группе не уходил. Лидера забираем на каждом шаге: пока он зомби, он
    числится в группе, и `killpg(pgid, 0)` не ответил бы «никого» до таймаута.
    Как только лидер вышел, остальным в его группе остаётся не больше
    `_ORPHAN_GRACE_S`.
    """
    pending: list[_Target] = []
    deadline = time.monotonic() + graceful_timeout
    for target in targets:
        try:
            target.kill(signal.SIGTERM)
        except ProcessLookupError:
            target.leader_gone()
            continue
        except OSError:
            pass
        target.deadline = deadline
        pending.append(target)
    while pending:
        now = time.monotonic()
        for target in list(pending):
            if target.leader_gone() and not target.killed:
                target.deadline = min(target.deadline, now + _ORPHAN_GRACE_S)
            if not _anyone_left(target.kill):
                pending.remove(target)
            elif now >= target.deadline:
                if target.killed:
                    pending.remove(target)     # SIGKILL не снял — ждать дальше нечего
                    continue
                with contextlib.suppress(OSError):
                    target.kill(signal.SIGKILL)
                target.deadline, target.killed = now + _KILL_WAIT_S, True
        if pending:
            time.sleep(0.1)


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
    """Остановить процесс и его группу/дерево по PID.

    Мёртвый лидер — выход без гашения, даже если его группа жива: номер из
    pid-файла мог достаться чужому процессу, а шелл — сам лидер своей группы, и
    сигнал группе по такому номеру погасил бы чужой терминал."""
    if not is_pid_alive(pid):
        return
    if os.name == "posix":
        # Номер группы — до сигнала: после смерти лидера `getpgid` уже падает,
        # а группа (его дети) ещё жива и как раз нужна.
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        except OSError:
            kill = functools.partial(os.kill, pid)      # группу не узнать — хотя бы сам процесс
        else:
            kill = functools.partial(os.killpg, pgid)
        _terminate_posix(
            [_Target(kill, lambda: _reap_child(pid))], graceful_timeout=graceful_timeout
        )
        return
    _terminate_windows([pid], graceful_timeout=graceful_timeout)


def _taskkill(pid: int, *, force: bool) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", *(["/F"] if force else [])],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def _terminate_windows(pids: list[int], *, graceful_timeout: float) -> None:
    """Тот же порядок на Windows: вежливый taskkill всем деревьям разом → общее
    ожидание → принудительный оставшимся. Групп POSIX там нет, живость — по pid."""
    for pid in pids:
        _taskkill(pid, force=False)
    deadline = time.monotonic() + graceful_timeout
    alive = [pid for pid in pids if is_pid_alive(pid)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.1)
        alive = [pid for pid in alive if is_pid_alive(pid)]
    for pid in alive:
        _taskkill(pid, force=True)


def terminate_processes(
    procs: Iterable[subprocess.Popen], *, graceful_timeout: float = 5.0
) -> None:
    """Погасить своих демонов разом — время не растёт с их числом
    (см. `_terminate_posix`)."""
    procs = list(procs)
    if os.name == "posix":
        # Демон — лидер своей сессии (`start_new_session`), номер группы равен
        # его pid. Пока лидер не забран — жив или зомби, — номер занят им, и
        # группу гасим и при мёртвом лидере: живы могут быть дети. Забранный
        # раньше лидер номер освободил, он мог достаться чужой группе — не трогаем.
        targets = [
            _Target(functools.partial(os.killpg, proc.pid), lambda proc=proc: proc.poll() is not None)
            for proc in procs
            if proc.returncode is None
        ]
        _terminate_posix(targets, graceful_timeout=graceful_timeout)
        return
    _terminate_windows(
        [proc.pid for proc in procs if proc.poll() is None], graceful_timeout=graceful_timeout
    )


def terminate_process(proc: subprocess.Popen, *, graceful_timeout: float = 5.0) -> None:
    terminate_processes([proc], graceful_timeout=graceful_timeout)


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
        managed = list(reversed(self._managed))
        for name, proc in managed:
            with contextlib.suppress(OSError):    # мёртвый терминал (EIO) не мешает гашению
                print(f"[stop] {name}: pid {proc.pid}")
        terminate_processes(proc for _, proc in managed)   # разом, а не по очереди
        for name, _ in managed:
            remove_pid(name, self.pids_dir)
        self._managed.clear()

    def kill_managed_now(self) -> None:
        """Последний рубеж аварийного выхода: SIGKILL всем своим демонам сразу, без
        ожидания и печати. Штатное гашение могло не уложиться в бюджет выхода —
        живыми их всё равно не оставляем. Группу уже забранного лидера не трогаем:
        его номер мог достаться чужому процессу."""
        for _, proc in list(self._managed):
            if proc.returncode is not None:
                continue
            if os.name == "posix":
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
                _taskkill(proc.pid, force=True)

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
