"""Метка личности процесса и идентификатор загрузки системы.

Метка — время старта процесса из ОС. Пара «pid и метка» узнаёт процесс
надёжнее одного pid: номер после выхода процесса достаётся другому, а
время старта у того другое. Перед сигналом процессу из сохранённого списка
метку сверяют — иначе сигнал уйдёт чужому процессу с тем же номером.

Linux — поле ``starttime`` из ``/proc/<pid>/stat``; macOS — ``lstart`` из
``/bin/ps`` с ``LC_ALL=C`` (вывод не зависит от локали). Утилиты — по
абсолютным путям: ``PATH`` бывает чужим (окружение сервера) или без
``/bin``. На других ОС метки нет.

Зомби хранит время старта до того, как родитель его пожмёт, и всё это
время держит свой pid и номер группы. ``process_start_states`` отдаёт метку
вместе с признаком «зомби» — для отбора групп под сигнал;
``process_start_mark`` зомби считает вышедшим и метки ему не даёт.

Время старта считается от загрузки системы (Linux) или с секундной
точностью (macOS), поэтому метку хранят вместе с ``boot_id``: после
перезагрузки сохранённые pid чужие.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

_PS = "/bin/ps"
_SYSCTL = "/usr/sbin/sysctl"
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


def parse_ps_table(text: str) -> dict[int, tuple[str, str]]:
    """{pid: (состояние, время старта)} из ``ps -o pid=,stat=,lstart=``.

    Пробелы времени схлопываются (``ps`` выравнивает день месяца);
    строки, которые не разобрать, пропускаются.
    """
    table: dict[int, tuple[str, str]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[0].isdigit():
            table[int(fields[0])] = (fields[1], " ".join(fields[2:]))
    return table


def process_start_states(pids: Iterable[int]) -> dict[int, tuple[str, bool]]:
    """{pid: (метка, зомби ли)} для найденных процессов; один опрос ОС
    (``ps`` macOS, отклонивший вызов, — по одному pid).

    Нет процесса, ошибка чтения, другая ОС — pid в ответе нет.
    """
    wanted = sorted({pid for pid in pids if pid > 0})
    if not wanted:
        return {}
    if sys.platform.startswith("linux"):
        raw = _linux_states(wanted)
    elif sys.platform == "darwin":
        raw = _ps_states(wanted)
    else:
        return {}
    return {
        pid: (start, state.startswith("Z"))
        for pid, (state, start) in raw.items()
        if start
    }


def process_start_mark(pid: int) -> str | None:
    """Метка живого процесса; зомби, нет процесса, другая ОС — ``None``."""
    found = process_start_states([pid]).get(pid)
    if found is None or found[1]:
        return None
    return found[0]


def boot_id() -> str:
    """Идентификатор текущей загрузки системы; не узнать — ``"0"``.

    macOS — ``kern.bootsessionuuid``: UUID загрузки, как ``boot_id`` Linux.
    """
    try:
        if sys.platform.startswith("linux"):
            path = Path("/proc/sys/kernel/random/boot_id")
            value = path.read_text(encoding="utf-8")
        elif sys.platform == "darwin":
            argv = [_SYSCTL, "-n", "kern.bootsessionuuid"]
            value = _run(argv, check=True).stdout
        else:
            return "0"
    except (OSError, subprocess.SubprocessError):
        return "0"
    return value.strip() or "0"


def _linux_states(pids: list[int]) -> dict[int, tuple[str, str]]:
    states: dict[int, tuple[str, str]] = {}
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            states[pid] = parse_linux_stat(stat)
        except (OSError, ValueError, IndexError):
            continue
    return states


def _ps_states(pids: list[int]) -> dict[int, tuple[str, str]]:
    """Один ``ps`` на все pid; отказ без вывода — по одному.

    Отсутствующие pid ``ps`` пропускает, но pid выше предела macOS
    отклоняет весь вызов с пустым выводом: одна битая запись не должна
    скрыть живые процессы остальных.
    """
    table = _ps_table(pids)
    if table is not None or len(pids) == 1:
        return table or {}
    states: dict[int, tuple[str, str]] = {}
    for pid in pids:
        states.update(_ps_table([pid]) or {})
    return states


def _ps_table(pids: list[int]) -> dict[int, tuple[str, str]] | None:
    """Таблица ``ps`` по pid; ``ps`` отказал без вывода — ``None``."""
    argv = [_PS, "-o", "pid=,stat=,lstart=", "-p", ",".join(map(str, pids))]
    try:
        result = _run(argv, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode and not result.stdout.strip():
        return None
    return parse_ps_table(result.stdout)


def _run(argv: list[str], *, check: bool) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_TOOL_TIMEOUT_SEC,
        env={**os.environ, "LC_ALL": "C"},
        check=check,
    )


__all__ = [
    "boot_id",
    "parse_linux_stat",
    "parse_ps_table",
    "process_start_mark",
    "process_start_states",
]
