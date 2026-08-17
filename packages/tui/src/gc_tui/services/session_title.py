"""Имя сессии: авто-имя из промпта + ручной title (единый источник для пикера и OSC).

Модель — два поля в `tui_meta` (читаются read-merge-write через FileTaskStore):
  - ``title``       — ручное переименование (Ctrl+R); пусто = сброс к авто-имени.
  - ``last_actual`` — авто-имя: первый промпт сеет безусловно, далее обновляется
                      только достаточно содержательным промптом (порог MIN).

display_name — единая приоритетная логика: title > last_actual > «Без названия».
Пикер (SessionsScreen) и OSC (TraceScreen) читают только её — никакого
сканирования jsonl/архивов для отображения имени.
"""
from __future__ import annotations

from typing import Mapping, Optional

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


def display_name(tui_meta: Optional[Mapping]) -> str:
    """Приоритетное имя сессии: title (ручной) > last_actual (авто) > «Без названия».

    Один источник истины для пикера (SessionsScreen._title_of) и OSC
    (TraceScreen._update_terminal_title).
    """
    meta = tui_meta or {}
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    last = meta.get("last_actual")
    if isinstance(last, str) and last.strip():
        return last.strip()
    return _UNNAMED
