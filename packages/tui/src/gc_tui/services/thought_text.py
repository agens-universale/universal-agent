"""Текст мысли модели для строки ленты (спека мыслей §2).

Модель может дописать в поле рассуждения служебную разметку своего
шаблона — например, `</thinking>` отдельными строками в конце. Это не
мысль: строки, где стоит только тег рассуждения, не показываются. Тег
посреди фразы остаётся — там он часть текста. Правило не зависит от
модели и провайдера.
"""

from __future__ import annotations

import re

_TAG_LINE = re.compile(r"\s*</?think(?:ing)?>\s*", re.IGNORECASE)


def thought_display_text(text: str) -> str:
    """Мысль без строк из одного тега `<think>`/`<thinking>` (открывающего
    или закрывающего); остальные строки — как есть."""
    kept = (line for line in text.split("\n") if not _TAG_LINE.fullmatch(line))
    return "\n".join(kept).strip()
