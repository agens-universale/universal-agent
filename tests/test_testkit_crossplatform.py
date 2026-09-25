"""Кроссплатформенные механизмы тест-кита: команда оболочки, симлинки, квант таймера.

Каждый закрывает целый класс Windows-падений, а не один тест: команда в
синтаксисе оболочки ЭТОЙ ОС (cmd.exe не понимает одинарных кавычек
`shlex.quote` и обрезает многострочный `-c`), честный пропуск там, где ОС не
даёт создать симлинк, и шаг системного таймера в критерии «loop свободен».
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import gc_testkit
from gc_testkit.helpers.crossplatform import python_command, require_symlinks, shell_command


def test_python_command_reaches_the_script_through_the_shell_of_this_os(tmp_path):
    """Аргументы с пробелом, апострофом и кириллицей доходят до скрипта целыми,
    а многострочный скрипт исполняется полностью."""
    args = ["with space", "it's", "кириллица"]
    script = "import json, sys\nline = json.dumps(sys.argv[1:])\nprint(line)\n"

    out = subprocess.run(
        python_command(tmp_path, script, *args),
        shell=True, capture_output=True, text=True, check=True,
    ).stdout

    assert json.loads(out) == args


def test_require_symlinks_skips_when_the_os_refuses(tmp_path, monkeypatch):
    """Windows без режима разработчика отказывает в симлинке (WinError 1314) —
    это свойство машины, а не поломка кода: тест пропускается с причиной."""
    def refuse(*_args, **_kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", refuse)

    with pytest.raises(pytest.skip.Exception, match="симлинк"):
        require_symlinks(tmp_path)


def test_timer_quantum_is_not_a_busy_loop(monkeypatch):
    """На Windows `asyncio.sleep(0.01)` просыпается по шагу таймера 15,6 мс.
    Ровный ряд таких тиков — свободный loop, а не «занят постоянно»."""
    monkeypatch.setattr(gc_testkit, "LOOP_TIMER_QUANTUM", 1 / 64)

    gc_testkit.assert_loop_was_free([1 / 64] * 50, tick=0.01)


def test_timer_quantum_does_not_hide_a_busy_loop(monkeypatch):
    """Квант поднимает пол шага, но не прячет занятый loop: медиана вдвое выше
    кванта по-прежнему красная."""
    monkeypatch.setattr(gc_testkit, "LOOP_TIMER_QUANTUM", 1 / 64)

    with pytest.raises(AssertionError, match="ПОСТОЯННО"):
        gc_testkit.assert_loop_was_free([0.032] * 50, tick=0.01)


@pytest.mark.parametrize("arg", ['print("a & b")', "echo %PATH%", "a|b", "a^b", "a>b"])
def test_shell_command_refuses_what_cmd_would_mangle(arg):
    """cmd.exe режет по `&|<>`, съедает `^`, раскрывает `%VAR%` и сбивает счёт
    кавычек на `\\"` из list2cmdline. Отказ — на ЛЮБОЙ ОС: автор теста на macOS
    узнаёт об этом сразу, а не от красного прогона на Windows."""
    with pytest.raises(ValueError, match="python_command"):
        shell_command(["python", "-c", arg])
