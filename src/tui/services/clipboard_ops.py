"""Единый write-path копирования во внешний буфер ОС.

OSC 52 + внутренний app.clipboard всегда (на UI-потоке); native через
os_ops.write_clipboard — синхронно (тесты) или в worker (TUI, O9).
Возврат bool = успех native при blocking; при async — True если задача поставлена.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from tui.services.os_ops import write_clipboard

MSG_COPIED = "Скопировано"
MSG_LINUX_DEPS = (
    "системный буфер недоступен — установите wl-clipboard или xclip"
)
MSG_SSH_OSC = "терминал должен разрешать OSC 52"


def is_ssh_session() -> bool:
    """True, если сессия под SSH (native пишет буфер сервера, не клиента)."""
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def with_source_cite(text: str, *, kind: str, path: str) -> str:
    """Дописать суффикс источника: ``[file: path]`` / ``[diff: path]``.

    Пустой text или path → text без изменений. Trailing whitespace текста
    срезается перед суффиксом (сам path не трогаем).
    """
    if not text:
        return text
    path = (path or "").strip()
    if not path:
        return text
    kind = (kind or "").strip() or "file"
    return f"{text.rstrip()}\n[{kind}: {path}]"


def copy_out(
    app: Any,
    text: str,
    *,
    toast: bool = False,
    blocking: bool = True,
) -> bool:
    """Скопировать text: OSC+internal сразу, native sync или в worker.

    toast=True — хоткей: при native ok показать «Скопировано».
    toast=False — copy-on-select: тихий успех.
    blocking=True — ждать write_clipboard (тесты / простой API).
    blocking=False — native в ``run_worker``; UI не ждёт subprocess (O9).
    При native fail — warning раз за сессию (текст зависит от среды).
    Пустой text — no-op, False.
    """
    if not text:
        return False
    app.copy_to_clipboard(text)
    if blocking or not callable(getattr(app, "run_worker", None)):
        ok = write_clipboard(text)
        _feedback(app, ok, toast=toast)
        return ok

    def _work() -> None:
        ok = write_clipboard(text)
        try:
            app.call_from_thread(_feedback, app, ok, toast=toast)
        except RuntimeError:
            # App уже останавливается — feedback не нужен.
            pass

    app.run_worker(_work, thread=True, name="clipboard-write")
    return True


def _feedback(app: Any, ok: bool, *, toast: bool) -> None:
    if ok:
        if toast:
            app.notify(MSG_COPIED, severity="information")
        return
    if getattr(app, "_clipboard_warn_shown", False):
        return
    hint = _fail_hint()
    if hint is None:
        return
    app._clipboard_warn_shown = True
    app.notify(hint, severity="warning")


def _fail_hint() -> str | None:
    """Текст warn при native fail; None = тихий fail (mac/win локально)."""
    if is_ssh_session():
        return MSG_SSH_OSC
    if sys.platform.startswith("linux"):
        return MSG_LINUX_DEPS
    return None
