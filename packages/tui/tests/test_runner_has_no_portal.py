"""Прогон агента в TUI идёт своим `asyncio.run`, а не через портал.

До этого шага цепочка была: поток UI (loop Textual) → рабочий поток →
синхронный `executor.run()` → ПОРТАЛЬНЫЙ loop в третьем потоке. То есть на
один прогон приходились три потока и два event loop, и один из них жил только
ради того, чтобы синхронное имя могло дождаться корутины.

Рабочий поток остаётся: агент не должен делить loop с Textual — любая
инлайн-операция ядра (файловый тул, гвард пути) заморозила бы интерфейс.
Уходит ровно портал.
"""
import asyncio
import threading

from gc_tui.services.agent_runner import _run_agent


class _Executor:
    def __init__(self) -> None:
        self.seen: dict = {}

    async def arun(self, msgs, **kw):
        self.seen["thread"] = threading.current_thread().name
        self.seen["loop"] = id(asyncio.get_running_loop())
        self.seen["msgs"] = msgs
        return "готово"


_ARGS = {
    "msgs": ["сделай что-нибудь"],
    "message_ids": [],
    "display_contents": [],
    "skills_loaded": [],
}


def test_run_agent_uses_its_own_loop_not_the_portal():
    """Прогон идёт на СВОЁМ loop, и портального потока не появляется."""
    from gc_core.utils.portal import shutdown_portal

    shutdown_portal()
    ex = _Executor()

    _run_agent(ex, _ARGS, thread_id="t1", start_new_thread=False, system="generic")

    assert ex.seen["msgs"] == ["сделай что-нибудь"]
    assert not any(
        t.name.startswith("gc-core-portal") for t in threading.enumerate()
    ), "прогон поднял портальный loop — значит где-то остался синхронный вход"


def test_run_agent_runs_on_the_calling_thread():
    """Свой loop, но в ТОМ ЖЕ потоке: `asyncio.run` не заводит новых.

    Рабочий поток заводит сам TUI (`AgentRunner`), и второго быть не должно —
    иначе `redirect_stdin`, потоко-локальный, перестал бы страховать
    чужой `input()`.
    """
    ex = _Executor()
    here = threading.current_thread().name

    _run_agent(ex, _ARGS, thread_id="t1", start_new_thread=False, system="generic")

    assert ex.seen["thread"] == here, (
        f"прогон уехал в поток {ex.seen['thread']!r} вместо {here!r}"
    )
