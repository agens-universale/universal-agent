"""Кроссплатформенные хелперы тестов: команда для оболочки ОС и симлинки.

Живут здесь, а не в каждом наборе: одинаковые заплатки по тестам расходились,
и новый тест снова писал команду в синтаксисе sh или создавал симлинк без
проверки — на Windows это падение, которое ничего не говорит о коде.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

#: Что cmd.exe не передаст программе как есть: режет по `&|<>`, съедает `^`,
#: раскрывает `%VAR%` даже в кавычках, а `\"` из list2cmdline сбивает ему счёт
#: кавычек.
_CMD_UNSAFE = frozenset('&|<>^%"')


def shell_command(argv) -> str:
    """argv → строка команды для оболочки ЭТОЙ ОС: cmd.exe на Windows, sh на POSIX.

    `shlex.quote` для cmd.exe не годится: одинарные кавычки там — обычные
    символы, и путь с пробелом разваливается на два аргумента.

    Аргументы с метасимволами cmd.exe отклоняются на ЛЮБОЙ ОС, а не
    экранируются: экранирование под cmd многослойно и без Windows не
    проверяется, а отказ на macOS сразу показывает автору теста, что на Windows
    команда развалится. Такой код кладите в скрипт через `python_command`.
    """
    argv = [str(arg) for arg in argv]
    unsafe = [arg for arg in argv if _CMD_UNSAFE & set(arg)]
    if unsafe:
        raise ValueError(
            f"cmd.exe не передаст как есть {unsafe!r}: символы {''.join(sorted(_CMD_UNSAFE))} "
            "в аргументах запрещены — код кладите в скрипт через python_command"
        )
    return (
        subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    )


def python_command(tmp_path, script: str, *args) -> str:
    """Команда «этот Python исполняет `script` с аргументами `args`».

    Скрипт кладётся ФАЙЛОМ в `tmp_path`, а не идёт через `-c`: cmd.exe обрезает
    многострочный аргумент на первом переводе строки.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", dir=tmp_path, delete=False, encoding="utf-8"
    ) as file:
        file.write(script)
    return shell_command([sys.executable, file.name, *args])


def require_symlinks(tmp_path) -> None:
    """Пропустить тест, если ОС не даёт создать симлинк.

    Windows без режима разработчика или прав администратора отвечает
    `WinError 1314`. Это свойство машины, а не кода, поэтому пропуск, а не
    падение. Проверяется настоящей попыткой, а не по имени ОС: там, где
    симлинки разрешены, тест должен работать и на Windows.
    """
    import pytest

    probe = Path(tmp_path) / ".symlink-probe"
    try:
        os.symlink(Path(tmp_path), probe, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"ОС не даёт создать симлинк: {exc}")
    else:
        probe.unlink()
