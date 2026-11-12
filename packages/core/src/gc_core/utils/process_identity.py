"""Метка личности процесса и идентификатор загрузки системы.

Метка — время старта процесса из ОС. Пара «pid и метка» узнаёт процесс
надёжнее одного pid: номер после выхода процесса достаётся другому, а
время старта у того другое. Перед сигналом процессу из сохранённого списка
метку сверяют — иначе сигнал уйдёт чужому процессу с тем же номером.

Linux — поле ``starttime`` из ``/proc/<pid>/stat``; macOS — ``lstart`` из
``ps`` с ``LC_ALL=C`` (вывод не зависит от локали). Зомби и отсутствующий
процесс метки не имеют: зомби уже вышел, его номер держится только до того,
как родитель его пожмёт. На других ОС метки нет (``None``).

Время старта считается от загрузки системы (Linux) или с секундной
точностью (macOS), поэтому метку хранят вместе с ``boot_id``: после
перезагрузки сохранённые pid чужие.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Потолок вызова ``ps`` и ``sysctl`` на macOS.
_TOOL_TIMEOUT_SEC = 5.0


def parse_linux_stat(text: str) -> tuple[str, str]:
    """(состояние, время старта) из содержимого ``/proc/<pid>/stat``.

    Имя процесса стоит в скобках и само может содержать пробелы и скобки,
    поэтому поля считаются от последней ``)``: за ней состояние (поле 3),
    время старта — поле 22.
    """
    fields = text[text.rindex(")") + 1 :].split()
    return fields[0], fields[19]


def parse_ps_line(text: str) -> tuple[str, str]:
    """(состояние, время старта) из вывода ``ps -o stat=,lstart= -p <pid>``.

    Пробелы времени схлопываются: ``ps`` выравнивает день месяца.
    """
    state, *start = text.split()
    return state, " ".join(start)


def process_start_mark(pid: int) -> str | None:
    """Метка личности процесса; зомби, нет процесса, другая ОС — ``None``."""
    if pid <= 0:
        return None
    try:
        if sys.platform.startswith("linux"):
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            state, start = parse_linux_stat(stat)
        elif sys.platform == "darwin":
            state, start = parse_ps_line(_ps_identity(pid))
        else:
            return None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    if state.startswith("Z") or not start:
        return None
    return start


def boot_id() -> str:
    """Идентификатор текущей загрузки системы; не узнать — ``"0"``."""
    try:
        if sys.platform.startswith("linux"):
            path = Path("/proc/sys/kernel/random/boot_id")
            value = path.read_text(encoding="utf-8")
        elif sys.platform == "darwin":
            value = _run(["sysctl", "-n", "kern.boottime"])
        else:
            return "0"
    except (OSError, subprocess.SubprocessError):
        return "0"
    return value.strip() or "0"


def _ps_identity(pid: int) -> str:
    return _run(["ps", "-o", "stat=,lstart=", "-p", str(pid)])


def _run(argv: list[str]) -> str:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_TOOL_TIMEOUT_SEC,
        env={**os.environ, "LC_ALL": "C"},
        check=True,
    )
    return result.stdout


__all__ = [
    "boot_id",
    "parse_linux_stat",
    "parse_ps_line",
    "process_start_mark",
]
