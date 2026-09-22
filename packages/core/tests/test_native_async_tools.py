"""Тулы, у которых ЕСТЬ нативная асинхронная форма, идут через неё.

Диспетчер уводит синхронный тул в ``asyncio.to_thread``. Для короткой работы
это просто дороже самой работы, и такие тулы переведены на инлайн
(``test_file_tools_run_inline.py``). Но четыре тула держат слот пула ДОЛГО, и
у каждого из них асинхронная форма существует:

===================  ==========================  ==================
тул                  чем занят                   нативная форма
===================  ==========================  ==================
``exec_sleep``       до 180 с ожидания           ``asyncio.sleep``
``exec_command``     до 120 с subprocess         ``backend.aexec``
``exec_python``      до 120 с subprocess         ``backend.arun_python``
``turn_ask_human``   ждёт человека без предела   ``await ctx.ask_human``
===================  ==========================  ==================

Пул ``to_thread`` — общий и небольшой (``min(32, cpu+4)``, на этой машине 14).
Тул, занявший слот на две минуты, отнимает его у всех остальных: замерено
20 параллельных ``exec_command`` — 4.09 с через пул против 2.10 с нативно.
Хуже того, отменённый ход слот не освобождает: ``asyncio.run`` на выходе
ждёт ``shutdown_default_executor()``, и прогон висел лишние 2.8 с.

Тот же приём уже применён к ``skills_run_script``
(``test_skills_run_script_native.py``).
"""
import asyncio
import json
import threading
import time

import pytest

from gc_core.tools.exec.sleep_tool import Sleep
from gc_core.tools.turn.ask_human import AskHuman
from gc_testkit.helpers.tool_ctx import local_ctx

#: Заметно больше шага пульса, чтобы заморозка не спряталась между тиками.
NAP = 0.4
TICK = 0.02


async def _worst_gap(coro_factory):
    """``(результат, худший разрыв пульса)`` за время работы корутины."""
    gaps: list = []
    stop = asyncio.Event()

    async def pulse() -> None:
        prev = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(TICK)
            now = time.perf_counter()
            gaps.append(now - prev)
            prev = now

    from gc_testkit import quiet_gc_for_loop_timing

    # Сборщик глушится на окне: порог абсолютный (NAP / 2 = 0,2 с), а пауза
    # поколения 2 на куче полного набора — 180–220 мс, прямо на пороге.
    # Пульс свой, но прогретый: 50 мс сна до работы дают ему взять отметку
    # раньше её начала.
    with quiet_gc_for_loop_timing():
        beat = asyncio.create_task(pulse())
        await asyncio.sleep(0.05)
        result = await coro_factory()
        stop.set()
        await beat
    return result, (max(gaps) if gaps else float("inf"))


# --- exec_sleep ------------------------------------------------------------


async def test_sleep_does_not_hold_the_loop():
    """``exec_sleep`` спит корутиной, не занимая loop."""
    tool = Sleep(ctx=None)

    out, gap = await _worst_gap(lambda: tool.acall(seconds=NAP))

    parsed = json.loads(out)
    assert parsed["ok"] is True, parsed
    assert parsed["slept_seconds"] == NAP
    assert gap < NAP / 2, f"loop залипал на {gap:.3f}с — сон не асинхронный"


async def test_sleep_does_not_occupy_a_pool_thread():
    """И не занимает слот общего пула — иначе 180 с потолка съедали бы его.

    Считать ``threading.active_count()`` здесь нельзя: под полным набором
    соседние тесты поднимают и гасят свои потоки, и счётчик шумит. Меряем то,
    ради чего слот и важен: пул забит, а сон всё равно начинается сразу.
    """
    tool = Sleep(ctx=None)

    hogs = [asyncio.create_task(asyncio.to_thread(time.sleep, 1.0)) for _ in range(24)]
    await asyncio.sleep(0.05)  # даём им занять все места

    started = time.perf_counter()
    out = json.loads(await tool.acall(seconds=0.05))
    spent = time.perf_counter() - started

    for h in hogs:
        h.cancel()
    await asyncio.gather(*hogs, return_exceptions=True)

    assert out["ok"] is True, out
    assert spent < 0.5, (
        f"сон ждал освобождения пула {spent:.2f}с — он всё ещё ходит в поток"
    )


async def test_sleep_actually_waits():
    """Асинхронность не должна превратить сон в мгновенный возврат."""
    tool = Sleep(ctx=None)

    started = time.perf_counter()
    await tool.acall(seconds=NAP)

    assert time.perf_counter() - started >= NAP * 0.9


@pytest.mark.parametrize(
    "seconds,field",
    [(None, "seconds"), ("нет", "seconds"), (-1, None), (10_000, None)],
)
async def test_sleep_validation_rejects_bad_arguments(seconds, field):
    """Проверки аргументов живут в единственной форме — `acall`."""
    tool = Sleep(ctx=None)

    out = json.loads(await tool.acall(seconds=seconds))

    assert out["ok"] is False


def test_sleep_has_no_sync_entry():
    """Синхронного входа у тула нет: форма одна, звать его нечем."""
    tool = Sleep(ctx=None)

    assert not callable(tool), "у Sleep снова появился синхронный вход"


# --- turn_ask_human --------------------------------------------------------


async def test_ask_human_awaits_an_async_port_on_the_calling_loop():
    """Корутинный порт встраивающего ждётся ЗДЕСЬ, а не на чужом loop.

    Синхронный путь гонит его через ``run_coro_blocking``, а тот при живом
    loop поднимает одноразовый поток с одноразовым event loop
    (``utils/aio_compat.py``). Порт встраивающего исполнялся на loop, которого
    приложение не создавало и не контролирует: всё, что порт держит привязанным
    к своему loop (очередь, соединение), там не работает.
    """
    seen = {}

    async def _port(question: str) -> str:
        seen["loop"] = asyncio.get_running_loop()
        return "ответ человека"

    ctx = local_ctx()
    ctx.ask_human = _port
    tool = AskHuman(ctx=ctx)

    out = await tool.acall(question="как дела?")

    assert seen["loop"] is asyncio.get_running_loop(), (
        "порт встраивающего исполнился на чужом loop"
    )
    assert "ответ человека" in str(out)


async def test_ask_human_still_accepts_a_sync_port():
    """Обычная функция как порт тоже принимается — контракт не сузился."""
    ctx = local_ctx()
    ctx.ask_human = lambda question: "синхронный ответ"
    tool = AskHuman(ctx=ctx)

    out = await tool.acall(question="как дела?")

    assert "синхронный ответ" in str(out)


async def test_find_in_files_runs_grep_through_the_async_backend(tmp_path):
    """``fs_find_in_files`` зовёт grep через ``aexec``, а не через пул.

    Пятый тул той же категории, приехал с develop. У него ДВА процессных
    бюджета — 120 с у нативного grep и 60 с у префильтра, — и оба он держал бы
    целиком: инлайн отнял бы loop у приложения, поток из пула занял бы слот.
    У запуска процесса нативная асинхронная форма есть, значит идём через неё.

    Проверяется факт маршрута, а не тайминг: подменяются ОБЕ формы бэкенда, и
    синхронная обязана остаться нетронутой.
    """
    from gc_core.tools.fs.find_in_files import FindInFiles

    (tmp_path / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")

    tool = FindInFiles(ctx=local_ctx())
    backend = tool._backend

    # Пробы возможностей греем ДО чтения свойств и ДО ловушек, все четыре.
    #
    # Порядок здесь несущий, и раньше он был обратным: свойства читались
    # первыми и запускали пробы САМИ, лениво и синхронно. Теперь свойство
    # отвечает только из кэша, а до прогрева отказывает — прочитать его первым
    # значит получить `CapabilitiesNotWarmedError` вместо ответа.
    #
    # Ловушка ниже сторожит МАРШРУТ САМОГО ПОИСКА, а не проб: без прогрева она
    # посчитала бы ещё и их запуски.
    await backend.awarm_capabilities()
    if not (backend.has_grep and backend.has_find):
        pytest.skip("в песочнице нет grep/find — нативного пути нет")

    seen = {"sync": 0, "async": 0}
    orig_aexec, orig_aexec_argv = backend.aexec, backend.aexec_argv

    async def _aexec(command, **kwargs):
        seen["async"] += 1
        return await orig_aexec(command, **kwargs)

    async def _aexec_argv(argv, **kwargs):
        seen["async"] += 1
        return await orig_aexec_argv(argv, **kwargs)

    def _sync_trap(*_a, **_k):
        seen["sync"] += 1
        raise AssertionError("нативный grep ушёл в синхронный exec")

    backend.aexec, backend.aexec_argv = _aexec, _aexec_argv
    backend.exec, backend.exec_argv = _sync_trap, _sync_trap
    try:
        out = await tool.acall(path=str(tmp_path), pattern="TOKEN", query_mode="text")
    finally:
        for name in ("aexec", "aexec_argv", "exec", "exec_argv"):
            try:
                delattr(backend, name)
            except AttributeError:
                pass

    data = json.loads(out)
    assert data["search_backend"] == "grep"
    assert seen["async"] >= 1, "асинхронная форма бэкенда не звалась"
    assert seen["sync"] == 0
