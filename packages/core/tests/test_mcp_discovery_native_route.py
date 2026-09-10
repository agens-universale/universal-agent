"""Дискавери MCP идёт нативно, когда сервер живёт в НАШЕМ процессе.

Маршрут выбирался по ``os_backend is None`` — условию грубее правила. Объект
бэкенда ставит ``acreate_run_context`` ВСЕГДА, поэтому нативная ветка
(``_adiscover_one_sdk``) не исполнялась никогда: весь обход MCP-серверов на
старте агента уходил в брошенный поток с синхронным httpx. Включая
``execute_on: agent`` — умолчание и подавляющее большинство серверов.

Правильный вопрос — не «есть ли объект бэкенда», а «говорим ли мы с хоста
агента». На него отвечает ``runs_in_agent_runtime`` тем же правилом, по
которому ``resolve_mcp_client`` выбирает реализацию: два места, знающие одно
правило, разъезжаются молча.

Беречь по-прежнему нужно сервер, который виден ТОЛЬКО изнутри песочницы:
``_adiscover_one_sdk`` ходит из нашего процесса и про песочницу не знает —
для такого сервера это «нет соединения», а вызовы тулов потом прекрасно
достучатся через ``OsBackendStatelessClient``. Симптом был бы: сервер молча
без тулов.
"""
import asyncio

import pytest

from gc_core.tools.connect_mcp import registry as reg
from gc_core.tools.connect_mcp.execute_on import runs_in_agent_runtime


class _Sandbox:
    is_sandbox = True


class _Local:
    is_sandbox = False


STREAM_CFG = {"transport": "stream_http", "url": "https://srv.test/mcp"}


def _routes(monkeypatch) -> list:
    """Записывает, какой веткой пошла дискавери."""
    taken: list = []

    async def _sdk(server_name, server_cfg, *, effective_transport, timeout):
        taken.append("нативная")
        return [], {}

    async def _abandoned(fn, *args, **kwargs):
        taken.append("поток")
        return [], {}

    monkeypatch.setattr(reg, "_adiscover_one_sdk", _sdk)
    monkeypatch.setattr(reg, "run_abandonable", _abandoned)
    return taken


async def test_agent_locus_goes_native_even_with_a_backend(monkeypatch):
    """ГЛАВНОЕ: наличие бэкенда больше не отменяет нативный путь."""
    taken = _routes(monkeypatch)

    await reg._adiscover_one("srv", dict(STREAM_CFG), timeout=5, os_backend=_Sandbox())

    assert taken == ["нативная"], (
        f"дискавери ушла в поток при execute_on: agent — {taken}"
    )


async def test_sandbox_locus_still_goes_through_the_backend(monkeypatch):
    """А сервер, видимый только из песочницы, беречь по-прежнему надо."""
    taken = _routes(monkeypatch)
    cfg = dict(STREAM_CFG, execute_on="os_backend")

    await reg._adiscover_one("srv", cfg, timeout=5, os_backend=_Sandbox())

    assert taken == ["поток"], (
        f"сервер из песочницы пошёл мимо бэкенда — {taken}: дискавери увидит "
        "«нет соединения», и сервер останется молча без тулов"
    )


async def test_sandbox_locus_without_a_sandbox_is_the_agent_runtime(monkeypatch):
    """`execute_on: os_backend` на НЕ-песочнице = исполняется у нас.

    Это то же исключение, что и в `resolve_mcp_client`: один конфиг обязан
    работать в обоих режимах.
    """
    taken = _routes(monkeypatch)
    cfg = dict(STREAM_CFG, execute_on="os_backend")

    await reg._adiscover_one("srv", cfg, timeout=5, os_backend=_Local())

    assert taken == ["нативная"], taken


def test_the_rule_lives_in_one_place():
    """Предикат и выбор клиента обязаны решать одинаково.

    Разъезд здесь молчалив: дискавери пойдёт одним маршрутом, а вызовы тулов
    другим, и сервер окажется «без тулов» при живом соединении.
    """
    from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient
    from gc_core.tools.connect_mcp.execute_on import resolve_mcp_client

    cases = [
        ({"transport": "http", "url": "u"}, None),
        ({"transport": "http", "url": "u"}, _Local()),
        ({"transport": "http", "url": "u"}, _Sandbox()),
        ({"transport": "http", "url": "u", "execute_on": "os_backend"}, None),
        ({"transport": "http", "url": "u", "execute_on": "os_backend"}, _Local()),
        ({"transport": "http", "url": "u", "execute_on": "os_backend"}, _Sandbox()),
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


async def test_loop_stays_responsive_during_discovery(monkeypatch):
    """Замер: пока идёт дискавери, соседний хартбит тикает.

    До правки обход уходил в брошенный поток целиком; сам loop при этом был
    свободен, но поток занимался на всё время обхода. Здесь проверяется
    следствие правки, ради которого она делалась: работа идёт НА LOOP и не
    занимает поток, а отзывчивость при этом не страдает.
    """
    slept = asyncio.Event()

    async def _sdk(server_name, server_cfg, *, effective_transport, timeout):
        await asyncio.sleep(0.2)
        slept.set()
        return [], {}

    def _thread_trap(*_a, **_kw):
        raise AssertionError("дискавери снова уводит работу в поток")

    monkeypatch.setattr(reg, "_adiscover_one_sdk", _sdk)
    monkeypatch.setattr(reg, "run_abandonable", _thread_trap)

    ticks = 0

    async def _heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(_heartbeat())
    try:
        await reg._adiscover_one("srv", dict(STREAM_CFG), timeout=5, os_backend=_Sandbox())
    finally:
        beat.cancel()

    assert slept.is_set()
    assert ticks >= 10, f"loop стоял во время дискавери: тиков {ticks}"
