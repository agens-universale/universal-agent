"""Единый write-path копирования во внешний буфер ОС.

Путь доставки один, не два: нативная запись средствами ОС (os_ops.write_clipboard)
— основной, OSC 52 через терминал — для SSH, для Linux и как запасной. Почему
их развели — см. `_send_osc52`. Внутренний app.clipboard наполняется всегда.
Возврат bool = успех native при blocking; при async — True если задача поставлена.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from gc_tui.services.os_ops import write_clipboard

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


def _remember_internally(app: Any, text: str) -> None:
    """Внутренний буфер приложения — из него читает Ctrl+V внутри TUI.

    Публичного сеттера у Textual нет: `App.clipboard` только для чтения, а
    `copy_to_clipboard` попутно отправляет OSC 52 — ровно то, чего мы здесь
    избегаем. Поэтому пишем поле напрямую."""
    try:
        app._clipboard = text
    except Exception:
        pass  # чужая реализация app — внутренний буфер не наша забота


def _send_osc52(app: Any, text: str) -> None:
    """Попросить терминал положить текст в буфер (управляющая последовательность).

    Дорогой путь на Windows: получив её, терминал сам захватывает системный
    буфер обмена, а тот эксклюзивен. Замер 2026-08-04 в Windows Terminal —
    захват занимал до 3-4 секунд, всё это время терминал не рисовал и не отдавал
    события (интерфейс выглядел зависшим), а параллельная нативная запись
    получала отказ. Поэтому на Windows и macOS шлём только как запасной путь;
    на Linux и в SSH — всегда (там он безопасен и/или единственный)."""
    send = getattr(app, "copy_to_clipboard", None)
    if not callable(send):
        return
    send(text)


def copy_out(
    app: Any,
    text: str,
    *,
    toast: bool = False,
    blocking: bool = True,
    quiet: bool = False,
    on_done: Callable[[bool], None] | None = None,
) -> bool:
    """Скопировать text наружу, выбрав ОДИН путь доставки.

    Путей два, и на Windows вместе они мешают друг другу (см. `_send_osc52`):
    нативная запись средствами ОС — основной, OSC 52 через терминал — всегда
    в SSH и на Linux, на прочих платформах только если нативная не удалась.

    toast=True — хоткей: при успехе показать «Скопировано».
    toast=False — copy-on-select: тихий успех.
    blocking=True — ждать write_clipboard (тесты / простой API).
    blocking=False — native в ``run_worker``; UI не ждёт запись (O9).
    При окончательной неудаче — warning раз за сессию (текст зависит от среды).
    quiet=True — без уведомлений приложения: говорит только вызывающий.
    on_done(ok or osc_sent) — итог доставки, в UI-потоке (при blocking=False — когда
    фоновая запись закончилась).
    Пустой text — no-op, False.
    """
    if not text:
        return False
    _remember_internally(app, text)

    # Два случая, когда OSC 52 нужен сразу, а не как запасной путь:
    #
    # SSH — нативная запись легла бы в буфер обмена СЕРВЕРА, до машины
    # пользователя текст так не доедет; OSC 52 несёт его через сам протокол.
    #
    # Linux — страховка от одноразовости X11: `xclip -loops 1` отдаёт содержимое
    # один раз и выходит, после первой вставки буфер пустеет (содержимое живёт в
    # процессе-владельце, а не в системе). Копия у терминала это закрывает.
    # Здесь OSC 52 безопасен: буфер X11/Wayland не эксклюзивный, пишущий никого
    # не блокирует — в отличие от Windows, где ровно это и вешало терминал.
    osc_sent = is_ssh_session() or sys.platform.startswith("linux")
    if osc_sent:
        _send_osc52(app, text)

    if blocking or not callable(getattr(app, "run_worker", None)):
        ok = write_clipboard(text)
        _finish_write(app, text, ok, osc_sent, toast, quiet, on_done)
        return ok

    def _work() -> None:
        ok = write_clipboard(text)
        try:
            app.call_from_thread(
                _finish_write, app, text, ok, osc_sent, toast, quiet, on_done
            )
        except RuntimeError:
            # App уже останавливается — feedback не нужен.
            pass

    app.run_worker(_work, thread=True, name="clipboard-write")
    return True


def _finish_write(
    app: Any,
    text: str,
    ok: bool,
    osc_sent: bool,
    toast: bool,
    quiet: bool,
    on_done: Callable[[bool], None] | None,
) -> None:
    """После нативной записи (UI-поток): запасной OSC 52, уведомление
    (если не quiet), итог вызывающему."""
    if not ok and not osc_sent:
        _send_osc52(app, text)
    if not quiet:
        _feedback(app, ok, toast=toast)
    if on_done is not None:
        # Для вызывающего текст дошёл, если сработал любой из путей.
        on_done(ok or osc_sent)


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
