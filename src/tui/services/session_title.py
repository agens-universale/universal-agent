"""Имя сессии: авто-имя из промпта + ручной title + task (единый источник для пикера и OSC).

Модель — поля в `tui_meta` (читаются read-merge-write через FileTaskStore):
  - ``title``       — ручное переименование (Ctrl+R); пусто = сброс к авто-имени.
  - ``task``        — агентская формулировка задачи из turn_finish.task.
  - ``last_actual`` — авто-имя: первый промпт сеет безусловно, далее обновляется
                      только достаточно содержательным промптом (порог MIN).

display_name — единая приоритетная логика:
title > task > last_actual > «Без названия».
Пикер (SessionsScreen) и OSC (TraceScreen) читают только её — никакого
сканирования jsonl/архивов для отображения имени.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

# Порог ОБНОВЛЕНИЯ last_actual (не seed): отсекает «ты тут?» / «ок» (≤30 символов),
# пропускает содержательные фразы. Считается по уже нормализованной строке.
LAST_ACTUAL_MIN = 30

# Потолок длины при записи (OSC/пикер не хранят простыню). Тоже по нормализованной.
LAST_ACTUAL_MAX = 200

_UNNAMED = "Без названия"


def normalize_prompt_title(text: str) -> str:
    """Collapse whitespace + truncate до LAST_ACTUAL_MAX.

    Пустой/чисто-пробельный текст → ``""`` (не пишем в last_actual).
    Порог и seed считаются по уже нормализованной строке.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:LAST_ACTUAL_MAX]


def _stripped(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def task_title(tui_meta: Optional[Mapping]) -> str:
    """Нормализованный ``meta.tui.task`` для заголовка (потолок LAST_ACTUAL_MAX)."""
    task = _stripped((tui_meta or {}).get("task"))
    if not task:
        return ""
    return " ".join(task.split())[:LAST_ACTUAL_MAX]


def display_name(tui_meta: Optional[Mapping]) -> str:
    """Приоритетное имя сессии: title > task > last_actual > «Без названия».

    Один источник истины для пикера (SessionsScreen._title_of) и OSC
    (TraceScreen._update_terminal_title).
    """
    meta = tui_meta or {}
    title = _stripped(meta.get("title"))
    if title:
        return title
    task = task_title(meta)
    if task:
        return task
    last = _stripped(meta.get("last_actual"))
    if last:
        return last
    return _UNNAMED


def task_subtitle(tui_meta: Optional[Mapping]) -> str:
    """Подзаголовок карточки F2: last_actual / task по приоритету заголовка.

    - заголовок = title → task, иначе last_actual;
    - заголовок = task → last_actual, если есть и отличается;
    - иначе пусто (дубликат не нужен).
    """
    meta = tui_meta or {}
    title = _stripped(meta.get("title"))
    task = task_title(meta)
    last = _stripped(meta.get("last_actual"))
    if title:
        return task or last
    if task:
        if last and last != task:
            return last
        return ""
    return ""
