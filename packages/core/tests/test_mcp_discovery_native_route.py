"""Дискавери MCP не занимает поток там, где сервер живёт в нашем процессе.

Маршрут выбирался по ``os_backend is None`` — условию грубее правила. Объект
бэкенда ставит ``acreate_run_context`` ВСЕГДА, поэтому нативная ветка не
исполнялась никогда: весь обход MCP-серверов на старте агента уходил в
брошенный поток с синхронным httpx. Включая ``execute_on: agent`` — умолчание
и подавляющее большинство серверов.

Форм дискавери было три: синхронная, корутинная для SDK-транспортов и
маршрутизатор над ними. Разбор конфига, whitelist, нормализация и оформление
ошибок жили в двух экземплярах. Теперь форма одна, а решение «как ходить в
сервер» принимает КЛИЕНТ: ``resolve_mcp_client`` отдаёт реализацию по паре
«место исполнения + вид бэкенда».

Здесь проверяется наблюдаемое: для сервера, живущего у нас, поток не
заводится; для видимого только из песочницы — маршрут через бэкенд сохранён.
"""

import asyncio
import threading

from gc_core.tools.connect_mcp import registry as reg
from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient
from gc_core.tools.connect_mcp.execute_on import (
    resolve_mcp_client,
    runs_in_agent_runtime,
)


class _Sandbox:
    is_sandbox = True


class _Local:
    is_sandbox = False


STREAM_CFG = {"transport": "stream_http", "url": "https://srv.test/mcp"}


def _no_threads(monkeypatch) -> list:
    """Считает потоки, заведённые за время вызова."""
    spawned: list = []
    real_start = threading.Thread.start

    def _counting(self):
        spawned.append(self.name)
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _counting)
    return spawned


async def test_discovery_spawns_no_thread_for_an_agent_runtime_server(
    monkeypatch,
):
    """ГЛАВНОЕ: наличие бэкенда больше не заставляет заводить поток."""

    async def _alist(self, *, timeout):
        return [{"name": "do_x", "description": "d"}]

    monkeypatch.setattr(AgentRuntimeClient, "alist_tools", _alist)
    spawned = _no_threads(monkeypatch)

    tools, status = await reg._adiscover_one(
        "srv", dict(STREAM_CFG), timeout=5, os_backend=_Sandbox()
    )

    assert status["status"] == "connected", status
    assert [t["name"] for t in tools] == ["do_x"], tools
    assert spawned == [], f"дискавери завела поток на пустом месте: {spawned}"


async def test_sandbox_server_still_goes_through_the_backend(monkeypatch):
    """Сервер, видимый только из песочницы, обязан идти через бэкенд.

    Ходить туда с хоста агента бессмысленно: дискавери увидит «нет
    соединения», сервер останется молча без тулов, а вызовы тулов потом
    прекрасно достучатся через `OsBackendStatelessClient`.
    """
    seen: list = []

    def _resolve(server_name, server_cfg, *, os_backend=None):
        seen.append(
            type(
                resolve_mcp_client(
                    server_name, server_cfg, os_backend=os_backend
                )
            ).__name__
        )

        class _Stub:
            async def alist_tools(self, *, timeout):
                return []

        return _Stub()

    monkeypatch.setitem(
        reg._adiscover_one.__globals__, "resolve_mcp_client", _resolve
    )
    cfg = dict(STREAM_CFG, execute_on="os_backend")

    await reg._adiscover_one("srv", cfg, timeout=5, os_backend=_Sandbox())

    assert seen == ["OsBackendStatelessClient"], (
        f"сервер из песочницы поехал не тем клиентом: {seen}"
    )


def test_the_locus_rule_lives_in_one_place():
    """Предикат и выбор клиента обязаны решать одинаково.

    Разъезд здесь молчалив: дискавери пойдёт одним маршрутом, а вызовы тулов
    другим, и сервер окажется «без тулов» при живом соединении.
    """
    cases = [
        ({"transport": "http", "url": "u"}, None),
        ({"transport": "http", "url": "u"}, _Local()),
        ({"transport": "http", "url": "u"}, _Sandbox()),
        ({"transport": "http", "url": "u", "execute_on": "os_backend"}, None),
        (
            {"transport": "http", "url": "u", "execute_on": "os_backend"},
            _Local(),
        ),
        (
            {"transport": "http", "url": "u", "execute_on": "os_backend"},
            _Sandbox(),
        ),
    ]
    for cfg, backend in cases:
        predicate = runs_in_agent_runtime(cfg, os_backend=backend)
        client = resolve_mcp_client("srv", cfg, os_backend=backend)
        actual = isinstance(client, AgentRuntimeClient)
        assert predicate == actual, (
            f"{cfg.get('execute_on', 'agent')} + "
            f"{'песочница' if getattr(backend, 'is_sandbox', False) else 'без песочницы'}: "
            f"предикат {predicate}, а клиент {type(client).__name__}"
        )


def test_discovery_has_exactly_one_form():
    """Форма ОДНА. Три копии разбора расходились бы молча."""
    import ast
    import inspect

    src = inspect.getsource(reg)
    tree = ast.parse(src)
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "discover_one" in n.name
    }
    assert names == {"_adiscover_one"}, (
        f"формы дискавери размножились: {names}"
    )


async def test_loop_stays_responsive_during_discovery(monkeypatch):
    """Пока идёт дискавери, соседний хартбит тикает."""

    async def _alist(self, *, timeout):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(AgentRuntimeClient, "alist_tools", _alist)

    ticks = 0

    async def _heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(_heartbeat())
    try:
        await reg._adiscover_one(
            "srv", dict(STREAM_CFG), timeout=5, os_backend=_Sandbox()
        )
    finally:
        beat.cancel()

    assert ticks >= 10, f"loop стоял во время дискавери: тиков {ticks}"
