"""Консольное одобрение плана: тот же выбор, что у баннера TUI, вопросом в терминале.

Без терминала (запуск без человека) одобрять некому: план остаётся финальным
ответом прогона.
"""

from __future__ import annotations

import asyncio
import json
import sys
from functools import partial
from typing import Any, Optional

from gc_core.message_policy.plan_messages import plan_accepted_message
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.plan_store import PLAN_STATUS_PROPOSED
from gc_core.security.permission_mode import (
    PLAN_EXECUTION_MODES,
    Mode,
    mode_label,
)

#: Ответ → режим исполнения. Номера и порядок — общие с баннером TUI.
_APPROVAL_MODES = {
    str(n): mode.value for n, mode in enumerate(PLAN_EXECUTION_MODES, start=1)
}
_REWORK = str(len(PLAN_EXECUTION_MODES) + 1)

_EXECUTE = {
    Mode.EDIT: "исполнить",
    Mode.AUTO: "исполнить автономно",
    Mode.MANUAL: "исполнить с подтверждениями",
}
_PROMPT = (
    "\nПлан предложен. "
    + "".join(
        f"[{n}] {_EXECUTE[mode]} ({mode_label(mode)}) / "
        for n, mode in enumerate(PLAN_EXECUTION_MODES, start=1)
    )
    + f"[{_REWORK}] доработать план / [N] выйти: "
)


async def _ask(prompt: str) -> Optional[str]:
    print(prompt, file=sys.stderr, end="", flush=True)
    try:
        return (await asyncio.to_thread(input)).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _plan_text(response: Any) -> str:
    try:
        return str(json.loads(response).get("plan") or "")
    except (TypeError, ValueError, AttributeError):
        return ""


async def arun_plan_approval(
    executor: Any,
    response: Any,
    *,
    thread_id: Optional[str],
    verbose: bool,
    account_run: bool = True,
) -> Any:
    """Спросить человека о предложенном плане; вернуть ответ последнего прогона.

    ``account_run=False`` — учёт прогона закрывает вызывающий (после
    перефраза `--format`). Тогда перед каждым продолжением прежний прогон
    закрывается здесь, а продолжение оставляет учёт открытым ему.

    Срабатывает только когда ИМЕННО этот прогон кончился планом: статус в
    сессии переживает прогоны, и по нему обычный запрос внезапно предложил бы
    одобрить давний план.
    """
    if not _approval_available(executor, thread_id):
        return response
    run = partial(
        _arun_in_mode,
        executor,
        thread_id=thread_id,
        verbose=verbose,
        account_run=account_run,
    )
    while True:
        plan = executor.plan_snapshot(thread_id)
        if not plan or plan.get("status") != PLAN_STATUS_PROPOSED:
            return response
        print(f"\n{_plan_text(response) or plan['text']}", file=sys.stderr)
        answer = await _ask(_PROMPT)
        mode = _APPROVAL_MODES.get(answer or "")
        if mode is not None:
            return await run(
                mode,
                [wrap_technical_user_content(plan_accepted_message(mode))],
                prepare=partial(executor.accept_plan, mode, thread_id),
            )
        if answer != _REWORK:
            return response
        feedback = await _ask("Что доработать в плане: ")
        if feedback:
            # Доработка — возврат к планированию; статус сменит сам прогон.
            response = await run(
                Mode.PLAN.value,
                [feedback],
                prepare=partial(executor.arm_plan_rework, thread_id),
            )


def _approval_available(executor: Any, thread_id: Optional[str]) -> bool:
    """Спрашивать есть кого и о чём: этот прогон кончился планом, есть
    терминал и сессия."""
    if getattr(executor, "last_terminal_outcome", None) != "plan_proposed":
        return False
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    if not thread_id:
        print(
            "Утверждение плана доступно только для сессии — перезапустите с -t.",
            file=sys.stderr,
        )
        return False
    return True


async def _arun_in_mode(
    executor: Any,
    mode: str,
    messages: list,
    *,
    prepare: Any,
    thread_id: Optional[str],
    verbose: bool,
    account_run: bool,
) -> Any:
    """Продолжение после ответа человека в выбранном режиме."""
    if not account_run:
        # До смены режима: учёт пишет мету прошлого прогона такой, какой она
        # была на его конце.
        await executor.aclose_run_accounting()
    executor.set_permission_mode(mode)
    prepare()
    return await executor.arun(
        messages,
        thread_id=thread_id,
        verbose=verbose,
        account_run=account_run,
    )
