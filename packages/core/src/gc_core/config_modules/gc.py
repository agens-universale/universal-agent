"""Резолвер флага ``gc.force_collect`` — управляет точкой C3 (collect после батча тулов).

C1 (после компакции) и C2 (после close_all в finally) — всегда включены и от флага
не зависят. C3 — условная точка: один ``gc.collect()`` на границе «батч тулов → LLM-шаг».

Значение режимное: ``True`` для CLI и TUI (долгоживущий / бенчмарк-режим),
``False`` для server (``gc.collect()`` глобален для процесса и стопает все треды).
Ключа ``gc.force_collect`` в схеме нет (строгая валидация его не пропустит),
поэтому прежнее чтение секции ``gc`` плоского конфига было мёртвым и снято.

См. ``docs_md/actual/2026-07-29-gc-collect-placement-spec.md``.
"""

from typing import Any


def gc_force_collect_enabled(
    settings: Any,
    *,
    is_tui: bool = False,
    is_cli_mode: bool = False,
) -> bool:
    """Включена ли точка C3 (collect после батча тулов).

    Секции ``gc`` в схеме нет — значение всегда режимное: CLI и TUI — да,
    server — нет. ``settings`` (контекст/настройки) не читается и оставлен
    ради сигнатуры.
    """
    del settings
    return is_cli_mode or is_tui
