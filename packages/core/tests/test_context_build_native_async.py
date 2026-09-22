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
        f"сборка подняла {peak[0] - before} потоков — похоже, вернулась форма to_thread(create_run_context)"
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

    from gc_testkit import LoopPulse, quiet_gc_for_loop_timing

    cfg = dict(_CFG)
    cfg["mcp-servers"] = {"srv": {"transport": "stream_http", "url": "http://x"}}
    # Сборщик глушится на окне: порог абсолютный, а пауза поколения 2 на куче
    # полного набора (180–220 мс) — почти весь бюджет в 0,25 с, и тест судил
    # бы её, а не сборку.
    #
    # Пульс прогретый и общий. Собственный ставился в очередь прямо перед
    # `await acreate_run_context` и не видел синхронную часть сборки до
    # первой уступки: 0,4 с сна перед `_prepare_run_context` тест пропускал —
    # проверено подменой.
    with quiet_gc_for_loop_timing():
        async with LoopPulse(step=0.005) as pulse:
            await acreate_run_context(cfg)

    assert pulse.worst() < 0.25, f"loop залипал на {pulse.worst():.3f}с при сборке контекста ({pulse.report()})"


def test_acreate_is_not_a_thread_wrapper():
    """Структурно: в ``acreate_run_context`` нет ``to_thread``.

    Поведенческие проверки выше зависят от того, какие транспорты в конфиге;
    эта краснеет на самой попытке вернуть обёртку над потоком.
    """
    src = (REPO / "packages/core/src/gc_core/run_context.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "acreate_run_context")
    calls = {
        getattr(c.func, "attr", None) or getattr(c.func, "id", None) for c in ast.walk(fn) if isinstance(c, ast.Call)
    }
    assert "to_thread" not in calls, (
        "acreate_run_context снова обёртка над потоком — 8 loop'ов и 10 потоков на сборку вместо одного"
    )


def test_context_assembly_has_exactly_one_form():
    """Сборка контекста должна быть ОДНА и корутинная.

    Раньше форм было две: синхронная `create_run_context` и корутинная. Тест
    на их месте следил, чтобы они не разъехались — делят ли они
    `_prepare_run_context` и `_finish_run_context`. Теперь стеречь нечего:
    синхронная снята, и правильный инвариант сильнее — второй формы быть не
    должно вовсе.

    Держалась синхронная ради TUI, а docs объясняли её привязкой MCP-сессий к
    loop'у. Обоснование было неверным: синхронная форма уводила дискавери в
    `run_coro_blocking`, то есть на такой же одноразовый loop, умиравший до
    первого хода. Настоящая причина была в том, что фабрика исполнителя в TUI
    — обычная функция без своего loop; теперь она заводит его сама.
    """
    src = (REPO / "packages/core/src/gc_core/run_context.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    sync_forms = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and "run_context" in n.name and not n.name.startswith("_")
    ]
    assert sync_forms == [], f"вернулась синхронная сборка контекста: {sync_forms}"

    build = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "acreate_run_context")
    calls = {
        getattr(c.func, "id", None) or getattr(c.func, "attr", None) for c in ast.walk(build) if isinstance(c, ast.Call)
    }
    for helper in ("_prepare_run_context", "_finish_run_context"):
        assert helper in calls, f"acreate_run_context не зовёт {helper}"
    assert "abuild_tools_data" in calls, "дискавери снова не ждётся"
    # Синхронной сборки тулов больше нет вовсе (шаг A2), но гейт остаётся:
    # он сторожит, чтобы она не вернулась. Слепое переименование однажды уже
    # превратило его в проверку наоборот.
    assert "build_tools_data" not in calls


def test_subagent_builds_use_the_async_form():
    """Субагенты собирают тулы асинхронно — они уже на loop.

    Синхронная форма там подняла бы ещё один loop в отдельном потоке
    (``run_coro_blocking``), а это ровно то, от чего уходили.
    """
    src = (REPO / "packages/core/src/gc_core/tools/subagents/runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {getattr(c.func, "id", None) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    assert "abuild_tools_data" in names
    assert "build_tools_data" not in names, "спавн субагента зовёт синхронную сборку тулов из-под работающего loop"
