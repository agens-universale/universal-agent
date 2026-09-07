"""Bang-shell: `!command` → локальный non-interactive shell в cwd TUI.

TUI-сервис: parse / preflight / Popen+read / truncate / cancel.
Не пишет в thread jsonl — только эфемерный результат для трейса.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from utils.is_number import is_number
from utils.win_console import preserve_console_mode

DEFAULT_MAX_OUTPUT_BYTES = 32768
DEFAULT_MAX_OUTPUT_LINES = 200

_INTERACTIVE_BINS = frozenset({
    "vim", "nvim", "vi", "nano", "emacs",
    "less", "more", "top", "htop", "btop", "watch",
})
_PRIVILEGE_TOKENS = frozenset({"sudo", "su", "doas", "pkexec", "runas"})
_TTY_CONTAINER_CMDS = frozenset({"docker", "podman", "nerdctl"})
_TTY_FLAGS = frozenset({"-it", "-ti", "--interactive", "--tty"})

_MSG_EMPTY = "пустая команда"
_MSG_DISABLED = "bang-shell выключен"
_MSG_BUSY = "дождитесь завершения предыдущей !"
_MSG_INTERACTIVE = (
    "интерактивные команды не поддерживаются — откройте внешний терминал"
)
_MSG_PRIVILEGE = (
    "привилегированные команды не поддерживаются — откройте внешний терминал"
)


@dataclass(frozen=True)
class BangShellSettings:
    enabled: bool = True
    timeout_sec: Optional[float] = None
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_output_lines: int = DEFAULT_MAX_OUTPUT_LINES

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "BangShellSettings":
        if not data:
            return cls()
        timeout = data.get("timeout_sec", None)
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                timeout = None
            if timeout is not None and timeout <= 0:
                timeout = None
        return cls(
            enabled=bool(data.get("enabled", True)),
            timeout_sec=timeout,
            max_output_bytes=int(data.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
            max_output_lines=int(data.get("max_output_lines", DEFAULT_MAX_OUTPUT_LINES)),
        )


@dataclass
class BangShellResult:
    command: str
    output: str = ""
    exit_code: Optional[int] = None
    cancelled: bool = False
    timed_out: bool = False
    rejected: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (
            self.rejected is None
            and not self.cancelled
            and not self.timed_out
            and self.exit_code == 0
        )


def parse_body(text: str) -> Optional[str]:
    """Тело после `!` (lstrip). None — пустая команда."""
    if not text.startswith("!"):
        return None
    body = text[1:].lstrip()
    return body if body else None


@dataclass(frozen=True)
class ClearMeta:
    """Meta `! clear` / `! clear N`: count=None — все блоки, иначе последние N."""

    count: Optional[int]  # None = all


def parse_clear_meta(body: str) -> tuple[Optional[ClearMeta], Optional[str]]:
    """Разобрать meta-clear. (None, None) — не clear; (meta, None) — ок; (None, err) — синтаксис."""
    parts = body.split()
    if not parts or parts[0].lower() != "clear":
        return None, None
    if len(parts) == 1:
        return ClearMeta(count=None), None
    if len(parts) != 2:
        return None, "использование: ! clear [N]"
    raw = parts[1]
    if raw.startswith("-") and len(raw) > 1 and is_number(raw[1:]):
        raw = raw[1:]
    if not is_number(raw):
        return None, "использование: ! clear [N]"
    n = int(raw)
    if n < 1:
        return None, "N должно быть ≥ 1"
    return ClearMeta(count=n), None


def normalize_output(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def truncate_output(
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_lines: int = DEFAULT_MAX_OUTPUT_LINES,
) -> str:
    """Truncate по байтам или строкам (что раньше). Binary/NUL → короткая заглушка."""
    if text.count("\x00") >= max(3, len(text) // 4):
        return "(binary output omitted)"

    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        kept = "".join(lines[:max_lines])
        return f"{kept}… (truncated, {len(lines)} lines)"

    raw = text.encode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        cut = raw[:max_bytes].decode("utf-8", errors="replace")
        return f"{cut}… (truncated, {len(raw)} bytes)"

    return text


def preflight(command: str) -> Optional[str]:
    """Отказ до spawn: interactive / privilege. None — можно запускать."""
    tokens = command.split()
    if not tokens:
        return _MSG_EMPTY

    for tok in tokens:
        base = os.path.basename(tok.rstrip("/\\"))
        if base.lower() in _PRIVILEGE_TOKENS or tok.lower() in _PRIVILEGE_TOKENS:
            return _MSG_PRIVILEGE

    leading = _leading_command(tokens)
    if leading is None:
        return None

    if leading.lower() in _INTERACTIVE_BINS:
        return _MSG_INTERACTIVE

    if leading.lower() in _TTY_CONTAINER_CMDS:
        if any(t in _TTY_FLAGS for t in tokens):
            return _MSG_INTERACTIVE

    return None


def _leading_command(tokens: list[str]) -> Optional[str]:
    """Первое «слово» команды: basename, с пропуском `env VAR=…` / `VAR=…`."""
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t == "env":
            i += 1
            while i < n and "=" in tokens[i] and not tokens[i].startswith("-"):
                i += 1
            continue
        if (
            "=" in t
            and not t.startswith("-")
            and not t.startswith("/")
            and "/" not in t.split("=", 1)[0]
        ):
            i += 1
            continue
        break
    if i >= n:
        return None
    return os.path.basename(tokens[i].rstrip("/\\"))


def _kill_process(proc: subprocess.Popen) -> None:
    """Кроссплатформенный kill: POSIX process group, иначе terminate/kill."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32" and getattr(proc, "pid", None):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass


class BangShellController:
    """Не больше одного concurrent bang; cancel из UI-потока безопасен."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._cancel_requested = False
        self._timed_out = False

    @property
    def is_running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            proc = self._proc
        if proc is not None:
            _kill_process(proc)

    def run(
        self,
        command: str,
        *,
        cwd: str,
        settings: Optional[BangShellSettings] = None,
    ) -> BangShellResult:
        """Синхронный spawn+wait (вызывать из фонового потока)."""
        settings = settings or BangShellSettings()
        command = (command or "").strip() if command else ""
        # тело уже без `!`; пустая строка — reject
        if not command:
            return BangShellResult(command="", rejected=_MSG_EMPTY)

        if not settings.enabled:
            return BangShellResult(command=command, rejected=_MSG_DISABLED)

        with self._lock:
            if self._running:
                return BangShellResult(command=command, rejected=_MSG_BUSY)
            self._running = True
            self._cancel_requested = False

        try:
            reason = preflight(command)
            if reason is not None:
                return BangShellResult(command=command, rejected=reason)

            return self._spawn_and_wait(command, cwd=cwd, settings=settings)
        finally:
            with self._lock:
                self._proc = None
                self._running = False

    def _spawn_and_wait(
        self,
        command: str,
        *,
        cwd: str,
        settings: BangShellSettings,
    ) -> BangShellResult:
        popen_kwargs: dict[str, Any] = dict(
            args=command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd or None,
            env=os.environ.copy(),
        )
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True

        with preserve_console_mode():
            try:
                proc = subprocess.Popen(**popen_kwargs)
            except OSError as exc:
                return BangShellResult(
                    command=command,
                    output=str(exc),
                    exit_code=127,
                )

            with self._lock:
                self._proc = proc
                self._timed_out = False

            deadline: Optional[float] = None
            if settings.timeout_sec is not None:
                deadline = time.monotonic() + settings.timeout_sec
                threading.Thread(
                    target=self._timeout_watch,
                    args=(proc, deadline),
                    daemon=True,
                ).start()

            # stdout.read() блокирует до EOF; cancel/timeout убивают proc → unblock.
            try:
                assert proc.stdout is not None
                raw = proc.stdout.read() or b""
            except Exception:
                raw = b""

            exit_code = proc.wait()

            with self._lock:
                cancelled = self._cancel_requested
                timed_out = self._timed_out and not cancelled
                self._timed_out = False

            text = normalize_output(raw.decode("utf-8", errors="replace"))
            text = truncate_output(
                text,
                max_bytes=settings.max_output_bytes,
                max_lines=settings.max_output_lines,
            )

            return BangShellResult(
                command=command,
                output=text,
                exit_code=None if (cancelled or timed_out) else exit_code,
                cancelled=cancelled,
                timed_out=timed_out,
            )

    def _timeout_watch(self, proc: subprocess.Popen, deadline: float) -> None:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            with self._lock:
                if self._cancel_requested:
                    return
            time.sleep(0.1)
        if proc.poll() is None:
            self._timed_out = True
            _kill_process(proc)
