"""Чужой синхронный бэкенд: отмена возвращает управление СРАЗУ.

Прервать чужой синхронный ``exec`` нечем — команда доработает до своего
таймаута в любом случае, и это заявленное ограничение совместимости. Но с
``asyncio.to_thread`` за неё платил ещё и вызывающий: отмена не возвращала
управление, потому что ``asyncio.run`` на выходе ждёт
``shutdown_default_executor``, то есть весь оставшийся таймаут команды. После
Esc в TUI это выглядело как «стоп не работает».

Второе свойство — пробы возможностей. ``has_grep``/``has_find`` остаются
синхронными свойствами (менять их значило бы править контракт ``FsBackend``
у каждого встраивающего), но платить за них на loop незачем: прогрев уводит
все четыре в один поток однократно на процесс.
"""

import asyncio
import threading
import time

import pytest
from gc_core.backends.async_protocol import aexec_port
from gc_testkit.settings import fake_ctx


class _SlowSyncBackend:
    """Чужой бэкенд: только синхронный exec, и он долгий."""

    def __init__(self, seconds: float = 5.0) -> None:
        self.seconds = seconds
        self.entered = threading.Event()
        self.finished = threading.Event()

    def exec(self, command, **kwargs):
        self.entered.set()
        time.sleep(self.seconds)
        self.finished.set()
        return {"ok": True}

    def exec_argv(self, argv, **kwargs):
        return self.exec(" ".join(argv))

    def run_python(self, code, **kwargs):
        return self.exec(code)


def test_cancelled_run_exits_without_waiting_for_the_command():
    """Прогон завершается сразу после отмены, не дожидаясь чужой команды.

    Мерить надо именно ВЫХОД `asyncio.run`, а не сам `await`: отмену
    ожидающей таски `asyncio.to_thread` отдаёт мгновенно и так. Цена там, где
    её не видно, — `asyncio.run` на выходе зовёт `shutdown_default_executor`
    и ждёт КАЖДЫЙ поток дефолтного пула. TUI гоняет ход своим `asyncio.run`,
    поэтому после Esc он висел ровно столько, сколько осталось команде.

    Брошенный поток этого не делает: он демонский и в пуле loop не числится.
    """
    backend = _SlowSyncBackend(seconds=5.0)

    async def _body():
        task = asyncio.create_task(aexec_port(backend).aexec("sleep 5"))
        await asyncio.to_thread(backend.entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(_body())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, (
        f"прогон вышел за {elapsed:.2f} с — выход ждал чужую команду"
    )
    assert not backend.finished.is_set(), (
        "проверка пустая: команда уже кончилась"
    )


async def test_the_thread_is_not_the_default_pool():
    """Поток брошенный: дефолтный пул держал бы выход loop на себе."""
    seen: list = []

    class _Naming(_SlowSyncBackend):
        def exec(self, command, **kwargs):
            seen.append(threading.current_thread().name)
            return {"ok": True}

    await aexec_port(_Naming(0)).aexec("echo")

    assert seen and "asyncio_" not in seen[0], (
        f"команда ушла в дефолтный пул ({seen[0]}) — отмена снова не вернётся"
    )


def test_sync_exec_does_not_share_the_mcp_pool():
    """Слоты свои: долгая команда не должна занимать пул MCP.

    Одна `execute_command` на чужом бэкенде идёт минутами. На пуле MCP она
    выбрала бы слоты, которыми пользуются дискавери и вызовы тулов СОСЕДНИХ
    агентов, — та же ошибка, которую уже исправляли для тел гвардов.
    """
    import ast
    import inspect

    from gc_core.backends import async_protocol
    from gc_core.tools.connect_mcp import abandonable

    assert async_protocol._EXEC_SLOTS is not abandonable._slots

    # Идентичности мало: общий пул вернётся не подменой объекта, а возвратом
    # к `run_abandonable` — точке входа, у которой набор слотов зашит внутри.
    src = inspect.getsource(async_protocol.SyncExecAdapter._off_loop)
    called = {
        getattr(c.func, "id", None) or getattr(c.func, "attr", None)
        for c in ast.walk(ast.parse(inspect.cleandoc(src)))
        if isinstance(c, ast.Call)
    }
    assert "run_abandonable" not in called, (
        "команды снова идут на пуле MCP: долгая команда займёт слоты дискавери"
    )
    assert "run_abandonable_in" in called


async def test_search_warms_the_probes_off_loop(monkeypatch, tmp_path):
    """Первый поиск греет пробы в потоке, а не читает их на loop."""
    from gc_core.tools.fs.find_in_files import FindInFiles

    calls: list = []

    class _Backend:
        async def awarm_capabilities(self):
            calls.append(asyncio.get_running_loop())

    tool = FindInFiles.__new__(FindInFiles)
    object.__setattr__(tool, "_backend_override", _Backend())
    # Тест про прогрев, не про политику: read_guard выключен явно (без
    # контекста гарды берут умолчания модели — включены).
    object.__setattr__(
        tool,
        "_ctx",
        fake_ctx(
            {"launch_root": str(tmp_path), "read_guard": {"enabled": False}}
        ),
    )
    monkeypatch.setattr(
        type(tool), "_backend", property(lambda self: self._backend_override)
    )

    async def _stub_dispatch(*a, **kw):
        return "{}"

    monkeypatch.setattr(type(tool), "_dispatch", _stub_dispatch)

    await tool.acall(path=str(tmp_path), pattern="x")

    assert calls, (
        "прогрев не позвали — первый поиск снова платит пробами на loop"
    )
