"""Жив ли процесс с данным PID — без сигнала ему и без psutil.

POSIX — ``kill(pid, 0)``: сигнал не шлётся, проверяется только существование.
Windows — ``OpenProcess``: получили хэндл — процесс есть. «Нет прав» в обоих
случаях значит «процесс есть, но чужой»: ошибаться выгодно в сторону «жив» —
вызывающие по «мёртв» удаляют чужие файлы.
"""

from __future__ import annotations

import os

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


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
    return _windows_pid_alive(pid)


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    # Своя копия kernel32, а не общий ``ctypes.windll.kernel32``: argtypes и
    # последняя ошибка не должны протекать в чужие вызовы того же процесса.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
