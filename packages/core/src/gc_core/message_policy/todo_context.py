"""Подача чеклиста ``session_todo`` модели: текстовые представления.

Владелец ответственности «как чеклист выглядит для модели». Тул хранит данные,
persistence их сохраняет, а здесь они превращаются в текст для двух каналов:

* блок ``<session_todo>`` в теле summary-сообщения — baseline после сжатия
  (дословно, как ``session_memory``: это единственный канал до модели, когда
  прошлые tool-call'ы уже вырезаны компакцией);
* тот же блок внутри ``<persistent_state>`` — вход суммаризатора;
* снапшот в тексте напоминания о протухшем чеклисте.

Статичного снимка в системном промпте намеренно НЕТ: он рендерился один раз на
старте рана и после сжатия посреди хода показывал устаревшее состояние.

Пустой чеклист рендерится в пустую строку: блока в промпте быть не должно.
"""

from __future__ import annotations

from typing import Any, Iterable

SNAPSHOT_MARKS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


def _rows(items: Any) -> list[str]:
    """Строки ``[x] текст`` для валидных пунктов; мусор пропускается."""
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes)):
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mark = SNAPSHOT_MARKS.get(item.get("status"))
        content = str(item.get("content") or "").strip()
        if mark is None or not content:
            continue
        out.append(f"{mark} {content}")
    return out


def format_todo_snapshot(items: Any) -> str:
    """Чеклист построчно, без обвязки. Пустой список → пустая строка."""
    return "\n".join(_rows(items))


def _done_of_total(items: Any) -> tuple[int, int]:
    rows = _rows(items)
    done = sum(1 for row in rows if row.startswith(SNAPSHOT_MARKS["completed"]))
    return done, len(rows)


def render_todo_for_persistent_state(items: Any) -> str:
    """Блок ``<session_todo>`` для ``<persistent_state>``. Пусто → пустая строка."""
    snapshot = format_todo_snapshot(items)
    if not snapshot:
        return ""
    return f"<session_todo>\n{snapshot}\n</session_todo>"
