"""Гейт: компакция больше не занимает поток из пула.

Зачем. Оркестрация суммаризации была синхронной, и цикл уводил её целиком в
``asyncio.to_thread`` через ``_acall_blocking_hook``. Event loop это не
блокировало (замер: 11.3 мс максимальная пауза за 6 с компакции), но воркер
дефолтного пула держался ВСЁ время суммаризации — десятки секунд. Слотов там
``min(32, cpu+4)``, и делят их все синхронные тулы: на сервере с несколькими
одновременными прогонами это упёрлось бы раньше всего остального.

После перевода оркестрации на async хуки координатора стали корутинными, а
``_acall_blocking_hook`` корутинную функцию ждёт НАТИВНО (там же, ветка
``inspect.iscoroutinefunction``) — потока не возникает вовсе.

Тест сторожит именно это свойство, а не форму реализации: важно, на каком
потоке оказалось тело хука, а не какими словами оно написано.
"""

import asyncio
import inspect
import threading

import pytest
from gc_core.processing_responses import _acall_blocking_hook


def _thread_name() -> str:
    return threading.current_thread().name


@pytest.mark.asyncio
async def test_async_hook_runs_on_the_loop_thread():
    """Корутинный хук исполняется прямо на loop — без прыжка в пул."""
    loop_thread = _thread_name()
    seen: list[str] = []

    async def ahook() -> None:
        seen.append(_thread_name())

    await _acall_blocking_hook(ahook)
    assert seen == [loop_thread], f"корутинный хук уехал в поток {seen} вместо loop-потока {loop_thread}"


@pytest.mark.asyncio
async def test_sync_hook_still_goes_to_a_thread():
    """Обратная совместимость: синхронный хук вызывающего по-прежнему в потоке.

    Встраивающие могли передать обычную функцию — её нельзя звать на loop, иначе
    мы вернём ровно ту заморозку, от которой уходили.
    """
    loop_thread = _thread_name()
    seen: list[str] = []

    def hook() -> None:
        seen.append(_thread_name())

    await _acall_blocking_hook(hook)
    assert seen and seen[0] != loop_thread, "синхронный хук исполнился на loop"


@pytest.mark.asyncio
async def test_loop_stays_free_while_hook_works():
    """И то и другое не должно морозить loop — проверяем пульсом."""
    ticks = 0
    stop = asyncio.Event()

    async def pulse() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    async def slow_ahook() -> None:
        await asyncio.sleep(0.2)

    beat = asyncio.create_task(pulse())
    await _acall_blocking_hook(slow_ahook)
    stop.set()
    await beat
    assert ticks >= 10, f"loop замер: всего {ticks} тиков за 0.2 с"


def test_coordinator_hooks_are_coroutines():
    """Хуки координатора обязаны быть корутинными — иначе всё вышеописанное зря.

    Именно эти методы уезжают в ``arun_agent`` аргументами
    (``pre_backend_compaction_hook``, ``turn_end_compaction_hook``,
    ``force_compact_hook``), и от их корутинности зависит, возьмёт
    ``_acall_blocking_hook`` нативную ветку или тредпул.
    """
    from gc_core.run_summary_coordinator import RunSummaryCoordinator

    expected = [
        "apre_run_compact_if_needed",
        "aforce_compact_once",
        "apre_backend_compaction_hook",
        "aturn_end_compact_if_needed",
    ]
    missing = [n for n in expected if not hasattr(RunSummaryCoordinator, n)]
    assert not missing, f"нет async-двойников: {missing}"

    not_async = [n for n in expected if not inspect.iscoroutinefunction(getattr(RunSummaryCoordinator, n))]
    assert not not_async, f"объявлены, но не корутинные: {not_async}"


def test_no_sync_twins_left():
    """Синхронных двойников у координатора НЕТ — форма одна, асинхронная.

    Раньше здесь стоял обратный сторож: двойники держали синхронный публичный
    API, а тот шёл через портал. Портала нет, обещания синхронного входа нет —
    и двойник, вернувшись, снова уведёт компакцию в поток на всё время
    LLM-вызова.
    """
    from gc_core.run_summary_coordinator import RunSummaryCoordinator

    for name in (
        "pre_run_compact_if_needed",
        "force_compact_once",
        "pre_backend_compaction_hook",
        "turn_end_compact_if_needed",
    ):
        assert not hasattr(RunSummaryCoordinator, name), f"вернулся синхронный {name}"
        assert inspect.iscoroutinefunction(getattr(RunSummaryCoordinator, "a" + name)), (
            f"a{name} перестал быть корутинным"
        )


def test_loop_receives_coroutine_hooks():
    """Цикл обязан получать КОРУТИННЫЕ хуки, а не синхронные имена.

    Эта проверка появилась не от хорошей жизни. Координатор перевели на async,
    а ``agent_executor`` продолжал передавать в ``arun_agent`` синхронные имена
    (``summary.pre_backend_compaction_hook`` вместо ``…a…``). Всё компилировалось,
    все прежние тесты были зелёными — и компакция как занимала поток из пула,
    так и занимала: ``_acall_blocking_hook`` видел обычную функцию и честно
    уводил её в ``asyncio.to_thread``.

    Поэтому проверяем не существование двойников (это делает тест выше), а то,
    что в цикл уезжают именно они.
    """
    import ast

    from gc_testkit.paths import REPO

    src = (REPO / "packages/core/src/gc_core/agent_executor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    hook_args = {
        "pre_backend_compaction_hook",
        "turn_end_compaction_hook",
        "force_compact_hook",
    }
    seen: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in hook_args and isinstance(kw.value, ast.Attribute):
                seen[kw.arg] = kw.value.attr

    missing = hook_args - set(seen)
    assert not missing, f"хуки не найдены в вызове arun_agent: {missing}"

    sync_names = {arg: attr for arg, attr in seen.items() if not attr.startswith("a")}
    assert not sync_names, (
        f"в цикл уезжают синхронные хуки {sync_names} — компакция снова займёт поток из пула; нужны корутинные двойники"
    )
