"""Проверка «жив ли процесс» — свой процесс, мёртвый ребёнок, мусорные PID."""

import os
import subprocess
import sys

from gc_core.utils.pid_alive import is_pid_alive


def test_own_process_is_alive():
    assert is_pid_alive(os.getpid()) is True


def test_reaped_child_is_dead():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    pid = child.pid
    del child  # на Windows хэндл держит процесс «живым»
    assert is_pid_alive(pid) is False


def test_non_positive_pid_is_dead():
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False
