"""Сохранение/восстановление режима ввода консоли Windows вокруг subprocess.

Запуск дочернего процесса на Windows сбрасывает флаг ENABLE_MOUSE_INPUT
родительской консоли. В TUI это значит: после первой же команды агента
(execute_command / run_python_code) Textual перестаёт получать события мыши —
клик/скролл уходят в выделение/scrollback терминала, и экран «залипает» до
перезапуска gc-ua. Оборачиваем subprocess в preserve_console_mode(), чтобы
вернуть режим (включая ENABLE_MOUSE_INPUT) сразу после команды.

Кросс-платформенно: вне Windows и при отсутствии консоли — no-op.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator, Optional

_STD_INPUT_HANDLE = -10


def _get_input_mode() -> Optional[int]:
    """Текущий режим ввода консоли, либо None (не Windows / нет консоли)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return int(mode.value)
    except Exception:
        return None


def _set_input_mode(mode: int) -> None:
    """Выставить режим ввода консоли. No-op вне Windows / при ошибке."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass


@contextmanager
def preserve_console_mode() -> Iterator[None]:
    """Восстановить режим ввода консоли после блока (вокруг subprocess).

    Поведение дочернего процесса не меняется — только возвращаем родительской
    консоли её режим после выполнения (subprocess на Windows гасит мышь)."""
    saved = _get_input_mode()
    try:
        yield
    finally:
        if saved is not None:
            _set_input_mode(saved)
