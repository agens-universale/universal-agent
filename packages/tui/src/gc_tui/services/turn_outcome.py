"""Словарь итогов хода: глиф и слова в одном месте.

Строка-итог индикатора ленты, статус в списке `/sessions` и заголовок
вкладки терминала читают одну таблицу глифов и слов — итог не
разъезжается (решение 23). Цвета у каждого места
свои. Фразы индикатора («Выполнил за 5с», «Ошибка», лимиты) и подписи
списка («✓ выполнено») живут рядом."""

from __future__ import annotations

from dataclasses import dataclass

from gc_tui.services.trace_glyphs import ERROR_GLYPH

#: Подпись успешного исхода хода. Ход, кончившийся предложенным планом,
#: работой не является: «Выполнил» под планом читалось бы как «сделано».
DONE_LABEL = "Выполнил"
DONE_LABEL_PLAN = "План готов"


@dataclass(frozen=True)
class Outcome:
    glyph: str
    #: Подпись в списке сессий (без глифа); None — в списке не показывается.
    list_label: str | None
    #: Фраза строки-итога индикатора; для «completed» — подпись по умолчанию.
    sentence: str


_LIMIT_GLYPH = "⏹"

OUTCOMES: dict[str, Outcome] = {
    "completed": Outcome("✓", "выполнено", DONE_LABEL),
    "failed": Outcome(ERROR_GLYPH, "ошибка", "Ошибка"),
    "cancelled": Outcome("■", "остановлено", "Отменено"),
    "running": Outcome("●", "идёт", ""),
    "asking": Outcome("?", "ждёт ответа", ""),
    "stopped_steps": Outcome(
        _LIMIT_GLYPH, None, "Остановлено: превышен лимит шагов"
    ),
    "stopped_errors": Outcome(
        _LIMIT_GLYPH, None, "Остановлено: превышен лимит ошибок"
    ),
    "stopped_tokens": Outcome(
        _LIMIT_GLYPH, None, "Остановлено: превышен лимит токенов"
    ),
    "stopped_tool_calls": Outcome(
        _LIMIT_GLYPH,
        None,
        "Остановлено: превышен лимит вызовов инструментов",
    ),
    "stopped_judge": Outcome(
        _LIMIT_GLYPH,
        None,
        "Остановлено: проверка действий недоступна — повторите позже "
        "или смените режим",
    ),
}

#: Другие слова того же исхода: статус в meta треда и состояние индикатора.
_ALIASES = {
    "done": "completed",
    "stopped": "cancelled",
    "unknown_failed": "failed",
}


def outcome_of(word: str | None) -> Outcome | None:
    """Исход по слову статуса или состоянию индикатора; незнакомое — None."""
    if not word:
        return None
    return OUTCOMES.get(_ALIASES.get(word, word))
