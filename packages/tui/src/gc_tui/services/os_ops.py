"""Кроссплатформенное чтение системного буфера обмена."""

from __future__ import annotations

import subprocess
import sys


def _run_capture(cmd: list[str]) -> str:
    """Запустить команду, вернуть stdout (utf-8) или '' при любой ошибке."""
    try:
        out = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=2.0,
        ).stdout
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    return out.decode("utf-8", errors="replace")


def _run_feed(cmd: list[str], text: str) -> bool:
    """Передать text в stdin команды (utf-8). True при успехе, False при ошибке."""
    try:
        result = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _win_read_clipboard() -> str:
    """Прочитать буфер обмена Windows через Win32 API (CF_UNICODETEXT).

    Без subprocess: запуск PowerShell на Windows сбрасывал кодовую страницу и
    ENABLE_MOUSE_INPUT консоли (спец-глифы TUI → '?', мышь отваливалась)."""
    try:
        import ctypes
        from ctypes import wintypes

        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not u32.OpenClipboard(None):
            return ""
        try:
            handle = u32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k32.GlobalUnlock(handle)
        finally:
            u32.CloseClipboard()
    except Exception:
        return ""


def _win_write_clipboard(text: str) -> bool:
    """Записать text в буфер обмена Windows через Win32 API (CF_UNICODETEXT).
    Без subprocess — см. _win_read_clipboard."""
    try:
        import ctypes
        from ctypes import wintypes

        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalFree.argtypes = [wintypes.HGLOBAL]

        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            k32.GlobalFree(h)
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.OpenClipboard(None):
            k32.GlobalFree(h)
            return False
        try:
            u32.EmptyClipboard()
            if not u32.SetClipboardData(_CF_UNICODETEXT, h):
                k32.GlobalFree(h)
                return False
            return True  # на успехе владение h переходит буферу — не освобождаем
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


def write_clipboard(text: str) -> bool:
    """Записать text в системный буфер обмена нативным средством ОС. True при успехе.

    Дополняет OSC 52 (`App.copy_to_clipboard`): нативный путь кладёт текст в буфер
    локальной машины независимо от поддержки OSC 52 терминалом (Terminal.app без
    OSC 52, корп-Windows и т.п.). OSC 52 остаётся для SSH/удалённых сессий.
    """
    if sys.platform == "win32":
        return _win_write_clipboard(text)
    if sys.platform == "darwin":
        return _run_feed(["pbcopy"], text)
    if sys.platform.startswith("linux"):
        # xclip без -loops 1 может держать процесс; timeout → ложный False.
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard", "-i", "-loops", "1"],
            ["xsel", "-b", "-i"],
        ):
            if _run_feed(cmd, text):
                return True
    return False


def read_clipboard() -> str:
    """Прочитать системный буфер обмена. Кроссплатформенно; '' если адаптер недоступен.

    Нужен как fallback для Ctrl+V в терминалах, доставляющих ctrl+v как key-event
    (а не bracketed-paste): встроенный TextArea.action_paste читает только
    внутренний app.clipboard, пустой для текста, скопированного вне приложения.
    """
    if sys.platform == "win32":
        return _win_read_clipboard().rstrip("\r\n")
    if sys.platform == "darwin":
        return _run_capture(["pbpaste"])
    if sys.platform.startswith("linux"):
        for cmd in (
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "-b"],
        ):
            text = _run_capture(cmd)
            if text:
                return text
    return ""
