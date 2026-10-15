"""Служебные сообщения модели о режиме разрешений: когда и что ей говорится.

Режим меняет человек, на ходу, а системный промпт у сессии один — поэтому о
режиме модель узнаёт сообщением в переписке. Правило одно на все режимы:
сравнивается режим, о котором модели уже сказано, с действующим. Сообщение
при выходе нужно там, где сообщение при входе утверждало то, что перестало
быть правдой. У edit и manual правил для модели нет — им нечего объявлять.

Тексты режима plan живут в ``plan_messages``; здесь — тексты auto и само
правило.
"""

from __future__ import annotations

from gc_core.message_policy.plan_messages import (
    PLAN_ENTER_MESSAGE,
    plan_cancel_message,
)
from gc_core.security.permission_mode import Mode

#: Вход в auto. Не утверждает ничего, что стало бы ложью после выхода: после
#: смены режима текст остаётся в истории.
AUTO_ENTER_MESSAGE = (
    "Permission mode 'auto' is active: the user chose autonomous work. Make "
    "reasonable assumptions on routine decisions and keep working; ask the "
    "user only when you are genuinely blocked — unclear direction, missing "
    "input, or a decision only the user can make. Commands, network requests, "
    "MCP tools, skill scripts and actions outside the project are reviewed "
    "automatically before they run. This is not a license for risky actions: "
    "do not delete data, change shared or production systems, or send "
    "anything outside — messages, tickets, uploads, secrets — unless the user "
    "asked for exactly that. If an action is blocked, do not retry it and do "
    "not reach the same result another way; continue with a materially safer "
    "alternative or with the rest of the task, and report what was blocked "
    "and why."
)

#: Режимы, у которых есть правила для модели, и текст их объявления.
_ENTER_MESSAGES = {
    Mode.PLAN.value: PLAN_ENTER_MESSAGE,
    Mode.AUTO.value: AUTO_ENTER_MESSAGE,
}


def auto_exit_message(mode: str) -> str:
    """Выход из auto: совет «работай без вопросов» больше не действует."""
    return (
        f"Permission mode 'auto' is off; the current mode is '{mode}'. "
        "Actions may now need the user's confirmation."
    )


def mode_boundary_messages(
    *, current: str, announced: str | None
) -> tuple[list[str], str | None]:
    """Сообщения о смене режима и то, какой режим теперь считается объявленным.

    Сравниваются состояния, а не переключения: несколько смен между прогонами
    схлопываются в один итог.
    """
    now_announced = current if current in _ENTER_MESSAGES else None
    if announced == now_announced:
        return [], now_announced
    messages: list[str] = []
    if announced == Mode.PLAN.value and current != Mode.PLAN.value:
        messages.append(plan_cancel_message(current))
    if announced == Mode.AUTO.value and now_announced is None:
        messages.append(auto_exit_message(current))
    if now_announced is not None:
        messages.append(_ENTER_MESSAGES[now_announced])
    return messages, now_announced


def render_mode_block(announced: str | None) -> str:
    """Правила объявленного режима для тела сводки: после сжатия истории
    сообщение о входе из контекста уходит, а режим продолжает действовать."""
    text = _ENTER_MESSAGES.get(str(announced or ""))
    if not text:
        return ""
    return f'<permission_mode name="{announced}">\n{text}\n</permission_mode>'
