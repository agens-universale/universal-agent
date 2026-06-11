"""Кроссплатформенное чтение системного буфера обмена."""
from __future__ import annotations

import subprocess
import sys


def _run_capture(cmd: list[str]) -> str:
    """Запустить команду, вернуть stdout (utf-8) или '' при любой ошибке."""
    try:
        out = subprocess.run(
            cmd, check=False, capture_output=True, timeout=2.0,
        ).stdout
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    return out.decode("utf-8", errors="replace")


def read_clipboard() -> str:
    """Прочитать системный буфер обмена. Кроссплатформенно; '' если адаптер недоступен.

    Нужен как fallback для Ctrl+V в терминалах, доставляющих ctrl+v как key-event
    (а не bracketed-paste): встроенный TextArea.action_paste читает только
    внутренний app.clipboard, пустой для текста, скопированного вне приложения.
    """
    if sys.platform == "win32":
        # Принудительно UTF-8: иначе PowerShell пишет stdout в OEM-кодировку
        # консоли (cp866 на ру-Windows) и кириллица превращается в кашу.
        text = _run_capture([
            "powershell", "-NoProfile", "-Command",
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
        ])
        return text.rstrip("\r\n")
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
