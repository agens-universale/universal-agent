"""Режимные резолверы явных ``gc.collect()``: C1/C2 и C3.

C1 (после компакции) и C2 (после close_all в finally) — везде, кроме TUI
(``gc_run_collect_enabled``). C3 — один ``gc.collect()`` на границе
«батч тулов → LLM-шаг» (``gc_force_collect_enabled``).

Оба значения режимные. C3: ``True`` только для CLI
(бенчмарк-режим); ``False`` для TUI (полная сборка на большой куче стопает
UI-цикл процесса интерфейса) и server (``gc.collect()`` глобален для
процесса и стопает все треды). Ключа ``gc.force_collect`` в схеме нет
(строгая валидация его не пропустит), поэтому прежнее чтение секции ``gc``
плоского конфига было мёртвым и снято.

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

    Секции ``gc`` в схеме нет — значение всегда режимное: только CLI.
    TUI стартует с ``system="cli"``, поэтому у него истинны оба флага
    (субагенты TUI наследуют оба) — отсюда ``and not is_tui``.
    ``settings`` (контекст/настройки) не читается и оставлен ради сигнатуры.
    """
    del settings
    return is_cli_mode and not is_tui


def gc_run_collect_enabled(*, is_tui: bool) -> bool:
    """Включены ли точки C1 (после компакции) и C2 (конец прогона).

    В TUI выключены: полная сборка в потоке агента держит GIL, и UI-цикл
    стоит (замер на окне ленты — медиана 180–280 мс, порог 100 мс).
    CLI и сервер собирают, как раньше. TUI стартует с ``system="cli"``,
    поэтому решает только ``is_tui``.
    """
    return not is_tui
