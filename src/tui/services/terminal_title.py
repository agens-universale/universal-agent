"""OS terminal window title (OSC) for the TUI — so the window is identifiable
in the taskbar / OBS window-capture by its title. Textual sets only its in-app
Header, not the OS title; we emit the OSC escape ourselves."""
from __future__ import annotations

import sys
from typing import Optional

_EMBLEM = "✦"   # U+2726
_APP_NAME = "gc-ua"


def format_terminal_title(
    task: Optional[str] = None,
    status: Optional[str] = None,
    *,
    max_len: int = 50,
) -> str:
    base = f"{_EMBLEM} {_APP_NAME}"
    if status:
        base = f"{base} · {status}"
    one_line = " ".join(task.split()) if task else ""
    if not one_line:
        return base
    if len(one_line) > max_len:
        one_line = one_line[:max_len] + "…"
    return f"{base} · {one_line}"


def set_terminal_title(text: str, *, out=None) -> None:
    """Write the OSC-0 window-title escape. `out` defaults to the real stdout
    (sys.__stdout__) so it works inside Textual's managed screen. No-op / safe
    if the stream is missing or write fails (OSC doesn't touch the screen grid).

    Prefer the binary `buffer` with explicit UTF-8: on Windows the text stream's
    encoding may be a legacy code page (cp1251), where writing the emblem `✦`
    raises UnicodeEncodeError — writing UTF-8 bytes bypasses that so the emblem
    actually reaches the terminal. Falls back to text write (e.g. StringIO)."""
    stream = out if out is not None else sys.__stdout__
    if stream is None:
        return
    data = f"\033]0;{text}\007"
    try:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(data.encode("utf-8"))
            buffer.flush()
        else:
            stream.write(data)
            stream.flush()
    except Exception:
        pass
