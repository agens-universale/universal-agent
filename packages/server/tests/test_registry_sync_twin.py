"""Синхронный двойник ``AgentRunRegistry.start_if_absent``.

Двойник заведён переездом на async и не имел ни одного вызывающего и ни одного
теста — а был сломан: ``run_coro_blocking`` исполнял ``astart_if_absent`` на
одноразовом loop, который ``asyncio.run`` закрывал сразу после возврата.
``astart_if_absent`` только СТАВИТ таску, поэтому прогон отменялся на первом же
``await``, не начавшись.
"""
import time

from gc_server.rest_api.agent_execution_registry import AgentRunRegistry


class _Executor:
    """Исполнитель, который действительно чего-то ждёт — как настоящий ход."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    async def arun(self, messages, **kwargs):
        import asyncio

        await asyncio.sleep(0.05)
        _Executor.seen.append(list(messages))
        return "готово"

    def add_message(self, message) -> None:
        pass


def _start(reg: AgentRunRegistry):
    return reg.start_if_absent(
        system="s",
        task_id="t1",
        executor_factory=_Executor,
        ctx=object(),
        initial_user_message="привет",
        thread_id="th1",
    )


def test_sync_twin_actually_runs_the_agent():
    """Прогон доходит до конца, а не отменяется вместе с временным loop."""
    _Executor.seen = []
    reg = AgentRunRegistry()

    run = _start(reg)
    assert run is not None

    deadline = time.monotonic() + 5
    while not _Executor.seen and time.monotonic() < deadline:
        time.sleep(0.02)

    assert _Executor.seen == [["привет"]], (
        "прогон не исполнился — таска умерла вместе с временным loop"
    )


def test_sync_twin_returns_the_registered_run():
    """Возвращается ActiveRun, и он же лежит в реестре активных."""
    _Executor.seen = []
    reg = AgentRunRegistry()

    run = _start(reg)

    assert run.system == "s"
    assert run.task_id == "t1"
    assert run.thread_id == "th1"
    assert reg._runs["s:t1"] is run
