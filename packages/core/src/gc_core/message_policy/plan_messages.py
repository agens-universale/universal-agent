"""Служебные сообщения модели о режиме plan и судьбе плана.

Английские — как все служебные сообщения модели. Тексты и правило «когда какое»
живут здесь; кто и в какой момент их шлёт — дело исполнителя и фронтендов.
"""

from __future__ import annotations

from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    PROPOSE_PLAN_TOOL_NAME,
)
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)

#: Вход в plan. Последняя фраза — короткий дубль правила «план уже предложен»:
#: носитель правила — описание тула, но сообщение шлётся один раз и после
#: сжатия истории может из контекста уйти.
PLAN_ENTER_MESSAGE = (
    "Permission mode 'plan' is active: explore the task and produce a plan. "
    "File edits, command execution, MCP calls and skill scripts are denied. "
    f"Propose the finished plan via {PROPOSE_PLAN_TOOL_NAME}. "
    "If a plan is already proposed and not yet reviewed, do not replace it silently: "
    "on the same task propose an updated plan keeping the earlier steps, otherwise ask "
    f"via {ASK_HUMAN_TOOL_NAME} first. To show the current plan again or let the "
    f"user approve it, call {PROPOSE_PLAN_TOOL_NAME} with show_current=true — do "
    "not retell the plan and do not say it is approved."
)


def plan_cancel_message(mode: str) -> str:
    """Выход из plan без одобрения плана; называет режим, в котором модель
    оказалась."""
    return (
        "Permission mode 'plan' is off; the pending plan was not approved. "
        f"The current mode is '{mode}'."
    )


#: Вход в plan для агента без тула плана (субагент): предложить план ему
#: нечем — он исследует и возвращает находки текстом в ответе.
PLAN_RESEARCH_ENTER_MESSAGE = (
    "Permission mode 'plan' is active: research only; edits, commands that "
    "change state and network are denied; return your findings as text in "
    "your answer."
)


def plan_research_exit_message(mode: str) -> str:
    """Выход из plan у агента без тула плана: о плане ему не говорилось."""
    return f"Permission mode 'plan' is off; the current mode is '{mode}'."


#: Человек убрал план командой. В истории и в сводке сжатия он ещё виден —
#: без этого сообщения модель продолжала бы считать его действующим.
PLAN_REMOVED_MESSAGE = (
    "The user removed the session plan. Any plan proposed earlier, including a "
    "<current_plan> block in the summary above, is no longer current: do not "
    "execute it or refer to it as pending."
)


def plan_resumed_message(mode: str) -> str:
    """План утверждён повторно: он мог быть выполнен частично."""
    return (
        f"The plan was approved earlier and may be partially done. Permission "
        f"mode is now '{mode}'. First check the current state of the project, "
        "then finish the remaining steps. If everything is already done, "
        "report that instead of redoing the work."
    )


def plan_accepted_message(mode: str) -> str:
    """План одобрен: режим сменён, можно исполнять."""
    return (
        f"The proposed plan is approved. Permission mode is now '{mode}'. "
        "Proceed with executing the plan."
    )


def accepted_plan_messages(
    mode: str, *, resumed: bool = False, extra_text: str = ""
) -> list[str]:
    """Сообщения продолжения после одобрения плана — одни на все
    фронтенды: служебное «план одобрен» (``resumed`` — «утверждён
    повторно, мог быть выполнен частично») и уточнение человека к
    исполнению отдельным сообщением, если оно не пусто."""
    text = (
        plan_resumed_message(mode) if resumed else plan_accepted_message(mode)
    )
    messages = [wrap_technical_user_content(text)]
    if extra_text.strip():
        messages.append(extra_text)
    return messages
