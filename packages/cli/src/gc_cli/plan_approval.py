"""Консольное одобрение плана: тот же выбор, что у баннера TUI, вопросом в терминале.

Без терминала (запуск без человека) одобрять некому: план остаётся финальным
ответом прогона.
"""

from __future__ import annotations

import asyncio
import sys
from functools import partial
from typing import Any, Optional

from gc_core.plan_store import PLAN_STATUS_PROPOSED, normalize_plan_block
from gc_core.security.permission_mode import (
    PLAN_EXECUTION_MODES,
    Mode,
    mode_label,
)
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey
from gc_core.tools.turn.propose_plan import ProposePlan

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
_EXECUTION_MODES = [mode.value for mode in PLAN_EXECUTION_MODES]

#: Ход кончился планом, а спросить некого (нет терминала): как исполнить.
_NO_TERMINAL_HINT = (
    "План предложен, сессия осталась в plan; исполнить: gc-cli -t "
    "{thread_id} --approve-plan "
    f"{_EXECUTION_MODES[0]} (или {', '.join(_EXECUTION_MODES[1:])})."
)

#: `--approve-plan`, а в сессии нечего одобрять.
NO_PROPOSED_PLAN = (
    "В сессии {thread_id} нет предложенного плана — одобрять нечего."
)


async def athread_has_proposed_plan(
    threads: Threads, thread_id: str, *, system: str = "cli"
) -> bool:
    """В сохранённой сессии план предложен и ждёт решения.

    `--approve-plan` идёт новым процессом: живого состояния прошлого
    прогона нет, план читается из состояния треда `(system, thread_id)`.
    """
    record = await threads.get(ThreadKey(system=system, thread=thread_id))
    plan = normalize_plan_block(record.state.plan) if record else None
    return plan is not None and plan["status"] == PLAN_STATUS_PROPOSED


async def _ask(prompt: str) -> Optional[str]:
    print(prompt, file=sys.stderr, end="", flush=True)
    try:
        return (await asyncio.to_thread(input)).strip()
    except (EOFError, KeyboardInterrupt):
        return None


async def arun_plan_approval(
    executor: Any,
    response: Any,
    *,
    thread_id: Optional[str],
    verbose: bool,
    account_run: bool = True,
    system: str = "cli",
) -> Any:
    """Спросить человека о предложенном плане; вернуть ответ последнего прогона.

    ``account_run=False`` — учёт прогона закрывает вызывающий (после
    перефраза `--format`). Прежний прогон закрывается до продолжения:
    при одобрении — последовательностью исполнителя
    (``aexecute_accepted_plan``), при доработке — здесь.

    Срабатывает только когда ИМЕННО этот прогон кончился планом: статус в
    сессии переживает прогоны, и по нему обычный запрос внезапно предложил бы
    одобрить давний план.
    """
    if not _approval_available(executor, thread_id):
        return response
    # Своя система треда (`--system`) — в каждый прогон; `cli` — умолчание
    # исполнителя, его не передаём.
    thread_system = {} if system == "cli" else {"system": system}
    run = partial(
        _arun_in_mode,
        executor,
        thread_id=thread_id,
        verbose=verbose,
        account_run=account_run,
        thread_system=thread_system,
    )
    while True:
        plan = executor.plan_snapshot(thread_id)
        if not plan or plan.get("status") != PLAN_STATUS_PROPOSED:
            return response
        shown = ProposePlan.present_result(response) or plan["text"]
        print(f"\n{shown}", file=sys.stderr)
        answer = await _ask(_PROMPT)
        mode = _APPROVAL_MODES.get(answer or "")
        if mode is not None:
            return await executor.aexecute_accepted_plan(
                mode,
                thread_id,
                verbose=verbose,
                account_run=account_run,
                **thread_system,
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
    сессия и терминал. Без терминала — подсказка, как одобрить флагом."""
    if getattr(executor, "last_terminal_outcome", None) != "plan_proposed":
        return False
    if not thread_id:
        print(
            "Утверждение плана доступно только для сессии — перезапустите с -t.",
            file=sys.stderr,
        )
        return False
    if not (sys.stdin and sys.stdin.isatty()):
        print(_NO_TERMINAL_HINT.format(thread_id=thread_id), file=sys.stderr)
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
    thread_system: dict[str, str],
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
        **thread_system,
    )
