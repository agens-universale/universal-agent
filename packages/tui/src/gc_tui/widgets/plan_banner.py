"""Баннер утверждения плана: показывается, когда ход закончился предложенным планом.

Тот же баннер с вариантами, что у вопросов подтверждения (`ConfirmBanner`):
рендер, выделение и выбор достаются наследованием, свои здесь только варианты.
Заголовок — сам вопрос, без значка: план с подписью «☰» стоит в рамке прямо
над баннером.
"""

from __future__ import annotations

from gc_core.security.permission_mode import PLAN_EXECUTION_MODES, mode_label

from gc_tui.services.mode_pill import MODE_HINTS
from gc_tui.widgets.confirm_banner import ConfirmBanner

#: Значение варианта «Обсудить и доработать»; остальные значения — режимы исполнения.
PLAN_CHOICE_REWORK = "rework"

#: (клавиша, подпись, значение, пояснение). Режимы исполнения держатся вместе,
#: действие — последним. Пояснение режима — то же, что футер показывает при его
#: включении: одно описание режима на весь продукт.
PLAN_OPTIONS: list[tuple[str, str, str, str]] = [
    (f"[{n}]", f"Да, в {mode_label(mode)}", mode.value, MODE_HINTS[mode.value])
    for n, mode in enumerate(PLAN_EXECUTION_MODES, start=1)
] + [
    # Действие говорит само за себя — пояснения нет.
    (
        f"[{len(PLAN_EXECUTION_MODES) + 1}]",
        "Обсудить и доработать",
        PLAN_CHOICE_REWORK,
        "",
    ),
]


_QUESTION = "Агент составил план и готов приступить к реализации. Выполнять?"
#: План уже утверждён и, возможно, частично выполнен: человек решает, идти ли
#: дальше, а не утверждает заново.
_QUESTION_RESUMED = "План уже утверждён. Продолжить выполнение?"


class PlanApprovalBanner(ConfirmBanner):
    def __init__(self, *, resumed: bool = False, **kwargs) -> None:
        #: План уже был утверждён: экран читает признак в момент выбора — от
        #: него зависит служебное сообщение модели при продолжении.
        self.resumed = resumed
        super().__init__(
            # Заголовок и есть вопрос: отдельная строка «Утверждение плана» над
            # ним только повторяла слово «план» (решение владельца).
            banner_title=_QUESTION_RESUMED if resumed else _QUESTION,
            question="",
            options=PLAN_OPTIONS,
            glyph="",
            hint="Esc — решить позже, /plan вернёт выбор",
            **kwargs,
        )
