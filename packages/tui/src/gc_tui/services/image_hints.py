"""Подсказки о картинках в футере (визуальный контракт картинок §2): что можно вставить
из буфера обмена и какой клавишей, как открыть вставленную картинку. Технический слой
без Textual: тексты и выбор клавиши по содержимому буфера; когда показывать — решает
экран."""

from __future__ import annotations

import sys

from gc_tui.services import os_ops
from gc_tui.services.image_paste import is_image_file
from gc_tui.services.os_ops import ClipboardProbe
from gc_tui.services.plural import plural_form

IMAGE_OPEN_TIP = "Ctrl+клик по плашке — открыть изображение"


def _images(n: int) -> str:
    """1 изображение · 2–4 изображения · 5+ изображений (11–14 — «изображений»)."""
    word = plural_form(n, "изображение", "изображения", "изображений")
    return f"{n} {word}"


def _paste_key(probe: ClipboardProbe, platform: str) -> str:
    """Клавиша, которая на этой системе вставит содержимое буфера плашками. macOS:
    картинка и скопированные в Finder файлы — Ctrl+V, а рядом с картинкой текст —
    Option+V (Ctrl+V вставил бы текст; имена рядом с файлами Ctrl+V не мешают).
    Windows: Ctrl+V забирает сам терминал — Alt+V. Linux совета не получает: буфер там
    без вставки не читается (`os_ops.probe_clipboard_image`)."""
    if platform != "darwin":
        return "Alt+V"
    return "Option+V" if probe.text else "Ctrl+V"


def clipboard_tip(probe: ClipboardProbe, *, platform: str) -> str | None:
    """Совет «в буфере изображение · клавиша — вставить»; None — вставлять картинками
    нечего. Скопированные файлы вставка берёт все или ни одного — среди них не
    картинка, совета нет. Читает заголовки файлов — звать вне UI-потока."""
    if probe.files:
        if not all(is_image_file(path) for path in probe.files):
            return None
        count = len(probe.files)
    elif probe.image:
        count = 1
    else:
        return None
    what = "изображение" if count == 1 else _images(count)
    return f"в буфере {what} · {_paste_key(probe, platform)} — вставить"


def read_clipboard_tip() -> tuple[str, str] | None:
    """Метка буфера и совет о нём; None — совета нет. Читает буфер и файлы — звать вне
    UI-потока."""
    probe = os_ops.probe_clipboard_image()
    tip = (
        clipboard_tip(probe, platform=sys.platform)
        if probe is not None
        else None
    )
    return (probe.stamp, tip) if tip is not None else None
