"""Буфер сессии: лимит 20k, prepend сверху, eviction снизу (SPEC 2026-07-25 §3.1 B4).

Канон (B4): новые куски кладём сверху, между кусками пустая строка. Лимит 20000
символов: когда не влезает — выкидываем самые старые куски снизу целиком; если
один кусок сам длиннее лимита — обрезаем его.

Буфер хранится как одна строка в `tui_meta.tui.copy_buffer`; куски разделены
двумя переводами строки (`\n\n`). Newest — сверху (первый кусок при split).
Модуль чисто функциональный, без I/O — запись в meta делает вызывающий код
из UI-потока Textual через `update_tui_meta`.
"""

from __future__ import annotations

COPY_BUFFER_MAX: int = 20000

# Разделитель кусков — пустая строка между ними.
_SEP: str = "\n\n"


def clamp_copy_buffer(text: str, *, max_size: int = COPY_BUFFER_MAX) -> str:
    """Уложить буфер в `max_size` символов по канону B4.

    - Куски — это то, что разделено `\n\n` (пустой строкой).
    - Если суммарно длиннее лимита — выкидываем самые старые куски снизу (с конца)
      целиком, пока не уложимся.
    - Если первый (новейший) кусок сам длиннее лимита — обрезаем его до лимита
      (остальное пропадает; ниже ничего не оставляем).
    - Пустой текст → "".
    """
    if not text:
        return ""
    if len(text) <= max_size:
        return text
    chunks = text.split(_SEP)
    # Если первый кусок длиннее лимита — отдаём только его префикс.
    if len(chunks[0]) > max_size:
        return chunks[0][:max_size]
    # Eviction снизу: накапливаем куски сверху, пока не превысим лимит.
    kept: list[str] = []
    total = 0
    for chunk in chunks:
        addition = len(chunk) + (len(_SEP) if kept else 0)
        if total + addition > max_size:
            break
        kept.append(chunk)
        total += addition
    return _SEP.join(kept)


def push_copy_buffer(
    existing: str,
    chunk: str,
    *,
    max_size: int = COPY_BUFFER_MAX,
) -> str:
    """Добавить `chunk` сверху к буферу `existing` и уложить в лимит.

    - Пустой `chunk` (после strip) — no-op, возвращаем existing как есть.
    - Иначе: prepend (newest сверху), между кусками `\n\n`, затем clamp.
    """
    chunk = chunk.strip("\n")  # убираем хвостовые/ведущие переводы строк куска
    if not chunk:
        return existing
    if not existing:
        merged = chunk
    else:
        merged = chunk + _SEP + existing
    return clamp_copy_buffer(merged, max_size=max_size)
