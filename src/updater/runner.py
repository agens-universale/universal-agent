"""Выполнение обновления. venv работающего процесса заменить нельзя (Windows-лок)
→ отвязанная shell-обёртка ждёт выхода родителя и зовёт `uv` напрямую (uv.exe вне
tool-venv). На Windows — в ВИДИМОМ окне-установщике (прогресс + «готово»), агент
не авто-стартует (юзер сам запускает, где удобно). Носитель — НЕ python
обновляемого venv."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable, Optional

import app_version
from updater.source import UpdateSource


def build_install_target(source: UpdateSource) -> str:
    """Строка переустановки ТЕМ ЖЕ профилем, что стоит сейчас.

    Агент ставится одним из четырёх дистрибутивов (`gc-ua` — всё, `gc-cli` —
    ядро, `gc-tui`, `gc-server`). Голый `git+URL` всегда резолвится в корневой
    манифест, то есть в полный профиль: без явного имени и подкаталога
    `--update` на headless-установке молча раздул бы её до полной, а на
    `gc-tui` — потерял бы TUI.
    """
    url = f"git+{source.url}" + (f"@{source.ref}" if source.ref else "")
    subdir = source.subdirectory or app_version.PROFILE_SUBDIRS.get(source.dist)
    if subdir:
        url = f"{url}#subdirectory={subdir}"
    # Мета живёт в корне: для неё голого URL достаточно, имя добавляем только
    # ради читаемости команды в логах.
    return f"{source.dist} @ {url}"


def build_install_command(source: UpdateSource, extra_args: list) -> list:
    uv = shutil.which("uv") or "uv"
    target = build_install_target(source)
    return [uv, "tool", "install", "--force", "--reinstall", target] + list(extra_args)


def install_location_phrase() -> str:
    """Где реально идёт detached-установка (I4): на Windows — в видимом консольном
    окне (`_spawn` — CREATE_NEW_CONSOLE), на остальных платформах — без консоли, в
    фоне (`start_new_session`). Единый источник для всех user-facing текстов
    update-flow (cli.py, tui/screens/settings.py), чтобы формулировка не врала про
    окно там, где его не будет."""
    return "в отдельном окне" if os.name == "nt" else "в фоне"


def _wait_pid_dead_snippet(pid: int) -> str:
    """POSIX sh: ждём, пока процесс pid исчезнет (kill -0)."""
    return f'while kill -0 {pid} 2>/dev/null; do sleep 0.5; done'


def _write_script(command: list, parent_pid: int, restart_cmd: Optional[list]) -> str:
    is_win = os.name == "nt"
    if is_win:
        cmd_str = subprocess.list2cmdline(command)
        # Видимое окно-установщик (спавнится с CREATE_NEW_CONSOLE): юзер видит прогресс
        # uv и момент «готово». Агент НЕ авто-стартует (решение юзера: не навязывать
        # окно/терминал — вдруг работает в IDE) → restart_cmd на Windows игнорируется,
        # пользователь сам запускает gc-ua, где удобно. chcp 65001 — чтобы кириллица в
        # echo рендерилась (файл в UTF-8; cmd читает построчно, смена кодовой до echo).
        body = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {parent_pid}" 2>NUL | find "{parent_pid}" >NUL\r\n'
            "if %ERRORLEVEL%==0 ( ping -n 2 127.0.0.1 >NUL & goto wait )\r\n"
            "echo Устанавливаю обновление gc-ua. Это займёт около минуты...\r\n"
            "echo.\r\n"
            f"{cmd_str}\r\n"
            "set RC=%ERRORLEVEL%\r\n"
            "echo.\r\n"
            'if "%RC%"=="0" (echo Готово. Запустите gc-ua заново, где вам удобно.)'
            ' else (echo Ошибка установки — подробности выше.)\r\n'
            "echo.\r\n"
            "echo Можно закрыть это окно.\r\n"
            "pause >nul\r\n"
        )
        fd, path = tempfile.mkstemp(suffix=f"-gc-update-pid{parent_pid}.cmd")
    else:
        cmd_str = shlex.join(command)
        restart_str = shlex.join(restart_cmd) if restart_cmd else ""
        body = (
            "#!/bin/sh\n"
            f"{_wait_pid_dead_snippet(parent_pid)}\n"
            f"{cmd_str}\n"
            + (f"{restart_str}\n" if restart_str else "")
        )
        fd, path = tempfile.mkstemp(suffix=f"-gc-update-pid{parent_pid}.sh")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    if os.name != "nt":
        os.chmod(path, 0o755)
    return path


def _spawn(script_argv: list) -> None:
    kwargs = {}
    if os.name == "nt":
        # CREATE_NEW_CONSOLE: отдельное ВИДИМОЕ окно — юзер видит прогресс установки и
        # завершение. Своя консоль (в отличие от DETACHED_PROCESS = ноль консоли, где
        # tasklist/find/ping в :wait-цикле не отрабатывали и переустановка не стартовала),
        # переживает выход родителя.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(script_argv, close_fds=True, **kwargs)  # noqa: S603


def spawn_detached_update(
    command: list,
    *,
    parent_pid: int,
    restart_cmd: Optional[list] = None,
    spawn: Callable[[list], None] = _spawn,
) -> None:
    path = _write_script(command, parent_pid, restart_cmd)
    argv = ["cmd", "/c", path] if os.name == "nt" else ["sh", path]
    spawn(argv)
