"""Обёртка отслеживания обязана иметь свой асинхронный exec.

Без него `aexec_port()` не находил у `TrackingFsBackend` ни `aexec`, ни
`aexec_argv`, ни `arun_python` и подставлял `SyncExecAdapter`. Дальше шла
цепочка: `to_thread(TrackingFsBackend.exec)` → `LocalFsBackend.exec` →
`run_coro_blocking(self.aexec)` → ЕЩЁ ОДИН event loop в потоке пула. То есть
КАЖДЫЙ `exec_command` стоил бы потока пула и вложенного loop.

Хуже цены — поведение на стопе: `to_thread` неотменяем, кооперативный стоп до
подпроцесса не доходил, а `asyncio.run` хода на выходе ждёт
`shutdown_default_executor`. После отмены хода поток жил бы до таймаута
команды.
"""

import asyncio

import pytest
from gc_core.backends.async_protocol import SyncExecAdapter, aexec_port
from gc_core.agent_edits.tracking import TrackingFsBackend


class _Inner:
    """Бэкенд с нативным асинхронным портом и без синхронного."""

    def __init__(self) -> None:
        self.loops: list = []

    async def aexec(self, command, **kw):
        self.loops.append(asyncio.get_running_loop())
        return {"ok": True, "command": command}

    async def aexec_argv(self, argv, **kw):
        self.loops.append(asyncio.get_running_loop())
        return {"ok": True, "argv": argv}

    async def arun_python(self, code, **kw):
        self.loops.append(asyncio.get_running_loop())
        return {"ok": True, "code": code}


class _Ctx:
    config_dict: dict = {}
    logger = None


def _wrapped():
    return TrackingFsBackend(_Inner(), _Ctx())


def test_wrapper_is_its_own_async_port():
    """Порт обязан выбрать саму обёртку, а не адаптер поверх синхронной ветки."""
    port = aexec_port(_wrapped())

    assert not isinstance(port, SyncExecAdapter), (
        "обёртка снова без aexec — каждый exec_command платит потоком и вложенным loop"
    )
    assert isinstance(port, TrackingFsBackend)


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.aexec("echo hi"),
        lambda b: b.aexec_argv(["echo", "hi"]),
        lambda b: b.arun_python("print(1)"),
    ],
    ids=["aexec", "aexec_argv", "arun_python"],
)
async def test_all_three_stay_on_the_callers_loop(call):
    """Команда идёт на loop вызывающего: ни второго loop, ни потока пула."""
    backend = _wrapped()

    result = await call(backend)

    assert result["ok"] is True
    assert backend._inner.loops == [asyncio.get_running_loop()], (
        "команда ушла на чужой loop — значит опять адаптер и вложенный asyncio.run"
    )


async def test_cancellation_reaches_the_command():
    """Стоп обязан доходить до подпроцесса, а не ждать его таймаута.

    Это и есть разница между нативным портом и `to_thread`: поток неотменяем,
    и после отмены хода он жил бы до конца команды.
    """
    started = asyncio.Event()
    cancelled: list = []

    class _Slow(_Inner):
        async def aexec(self, command, **kw):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    backend = TrackingFsBackend(_Slow(), _Ctx())
    task = asyncio.create_task(backend.aexec("sleep 30"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled, "отмена не дошла до команды"
