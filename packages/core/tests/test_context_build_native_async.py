"""Гейт: сборка контекста асинхронна нативно, а не через поток.

``acreate_run_context`` была обёрткой ``asyncio.to_thread(create_run_context)``.
Работало — loop оставался свободен, — но стоило дорого. Замер давал **8 event
loop'ов и пик в 10 потоков на ОДНУ сборку**: воркер дефолтного пула, внутри
него ``asyncio.run``, а под ним по демон-потоку на каждый MCP-сервер и ещё по
``asyncio.run`` в каждом.

И тянуло ловушку: отмена ожидания возвращала управление не сразу, а через сотни
миллисекунд — ``asyncio.run`` дожидался брошенного воркера дефолтного пула
(``shutdown_default_executor``). Это ровно то, что уровнем ниже запрещает
``abandonable``, а на уровне контекста возвращалось обратно.

Дорогая часть здесь одна — MCP-дискавери (866–1405 мс против 2.4–6.9 мс на всё
остальное), и async-двойники сделаны ровно под неё.
"""
import ast
import asyncio
import threading

import pytest

from gc_testkit.paths import REPO

_CFG = {"stop_tools": [], "backend_api_mode": "chat_completions", "save_logs": False}


@pytest.mark.asyncio
async def test_build_does_not_spawn_threads():
    """Главная проверка: сборка не плодит потоки.

    Считаем пик живых потоков вокруг вызова. Прежняя форма поднимала воркер
    пула и по демон-потоку на сервер; нативная не поднимает ничего, пока в
    конфиге нет легаси-транспортов.
    """
    from gc_core.run_context import acreate_run_context

    before = threading.active_count()
    peak = [before]
    stop = asyncio.Event()

    async def watch() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], threading.active_count())
            await asyncio.sleep(0.002)

    w = asyncio.create_task(watch())
    ctx = await acreate_run_context(dict(_CFG))
    stop.set()
    await w

    assert ctx.tools_data, "контекст собрался пустым — тест ничего не проверил"
    assert peak[0] - before <= 1, (
        f"сборка подняла {peak[0] - before} потоков — похоже, вернулась форма "
        f"to_thread(create_run_context)"
    )


@pytest.mark.asyncio
async def test_loop_stays_free_during_build(monkeypatch):
    """Пульс не спотыкается, пока контекст собирается.

    Дискавери подменяем медленной: без MCP-серверов сборка укладывается в
    единицы миллисекунд и заканчивается раньше первого тика — мерить было бы
    нечего. Заглушка асинхронная и «долгая» ровно так, как ведёт себя настоящая
    сеть.
    """
    import gc_core.tools.connect_mcp.agent_tools as agent_tools
    from gc_core.run_context import acreate_run_context

    async def _slow(config_dict, on_progress=None, **_kw):
        await asyncio.sleep(0.2)
        return {}, {}

    monkeypatch.setattr(agent_tools, "aget_mcp_tools_data_for_config", _slow)

    gaps: list[float] = []
    stop = asyncio.Event()

    async def pulse() -> None:
        loop = asyncio.get_running_loop()
        prev = loop.time()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = loop.time()
            gaps.append(now - prev)
            prev = now

    cfg = dict(_CFG)
    cfg["mcp-servers"] = {"srv": {"transport": "stream_http", "url": "http://x"}}
    beat = asyncio.create_task(pulse())
    await acreate_run_context(cfg)
    stop.set()
    await beat

    assert gaps, "пульс не тикнул ни разу"
    assert max(gaps) < 0.25, f"loop залипал на {max(gaps):.3f}с при сборке контекста"


def test_acreate_is_not_a_thread_wrapper():
    """Структурно: в ``acreate_run_context`` нет ``to_thread``.

    Поведенческие проверки выше зависят от того, какие транспорты в конфиге;
    эта краснеет на самой попытке вернуть обёртку над потоком.
    """
    src = (REPO / "packages/core/src/gc_core/run_context.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "acreate_run_context"
    )
    calls = {
        getattr(c.func, "attr", None) or getattr(c.func, "id", None)
        for c in ast.walk(fn) if isinstance(c, ast.Call)
    }
    assert "to_thread" not in calls, (
        "acreate_run_context снова обёртка над потоком — 8 loop'ов и 10 потоков "
        "на сборку вместо одного"
    )


def test_both_context_forms_share_prepare_and_finish():
    """Синхронная и асинхронная сборка обязаны делить подготовку и досборку.

    Иначе они разъедутся: у одной появится шаг, которого нет у другой, и
    поведение CLI разойдётся с поведением сервера. Такое уже случалось —
    см. раздел про расхождения в плане.
    """
    src = (REPO / "packages/core/src/gc_core/run_context.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    for name, kind in (
        ("create_run_context", ast.FunctionDef),
        ("_acreate_run_context_impl", ast.AsyncFunctionDef),
    ):
        fn = next(n for n in ast.walk(tree) if isinstance(n, kind) and n.name == name)
        calls = {
            getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            for c in ast.walk(fn) if isinstance(c, ast.Call)
        }
        for helper in ("_prepare_run_context", "_finish_run_context"):
            assert helper in calls, f"{name} не зовёт {helper}"


def test_subagent_builds_use_the_async_form():
    """Субагенты собирают тулы асинхронно — они уже на loop.

    Синхронная форма там подняла бы ещё один loop в отдельном потоке
    (``run_coro_blocking``), а это ровно то, от чего уходили.
    """
    src = (
        REPO / "packages/core/src/gc_core/tools/subagents/runner.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {
        getattr(c.func, "id", None)
        for c in ast.walk(tree) if isinstance(c, ast.Call)
    }
    assert "abuild_tools_data" in names
    assert "build_tools_data" not in names, (
        "спавн субагента зовёт синхронную сборку тулов из-под работающего loop"
    )
