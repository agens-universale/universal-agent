"""Пробы возможностей бэкенда: прогрев обязателен и не занимает loop.

Свойства ``has_grep`` / ``has_find`` / ``grep_exclude_dir`` / ``grep_include``
читают ТОЛЬКО кэш. Наполняет его единственный писатель — корутина
``awarm_capabilities``; кто её не позвал, получает
``CapabilitiesNotWarmedError`` (см. ``test_backend_capabilities``).

Так сделано не ради красоты формы. Раньше свойство запускало пробу само,
лениво и синхронно: первая же ``fs_find_in_files`` платила за четыре запуска
процесса (до ~10 с на пробу в медленной песочнице) ПРЯМО НА event loop
вызывающего. Агент выглядел зависшим на первом инструменте, и никакого следа в
логах у этого не было. Корутиной свойство не сделаешь, поэтому пробу из него
вынули целиком.

Прогрев идёт параллельно MCP-дискавери, которая и так занимает 866–1405 мс,
поэтому на время сборки контекста он не влияет.
"""

import asyncio
import time

import pytest
from gc_core.backends import capabilities as capabilities
from gc_core.backends.local import LocalFsBackend

_CACHES = (
    "_has_find_cache",
    "_has_grep_cache",
    "_grep_exclude_dir_cache",
    "_grep_include_cache",
)


async def test_warm_capabilities_fills_all_four_caches():
    backend = LocalFsBackend()
    assert all(getattr(backend, c) is None for c in _CACHES), "кэш уже прогрет"

    await backend.awarm_capabilities()

    cold = [c for c in _CACHES if getattr(backend, c) is None]
    assert cold == [], f"после прогрева остались холодными: {cold}"


async def test_awarm_capabilities_does_not_block_the_loop():
    """Прогрев ЖДЁТ пробы, а не держит на них поток loop'а.

    Пробы подменены медленными, но корутинными: ``await asyncio.sleep`` вместо
    запуска процессов. Живая проба на этой машине занимает ~70 мс на все
    четыре — на таком сигнале заморозку от планировщика не отличить, и
    прежняя версия этого теста проверяла лишь «пульс вообще тикал», то есть
    зеленела на любой реализации, включая морозящую.

    Здесь сигнал задан: 0.2 с на каждую из двух проб. Если прогрев вернётся к
    ``run_coro_blocking`` / ``to_thread(синхронный прогрев)``, очередь loop'а
    встанет на всё это время и разрыв пульса вырастет до сотен миллисекунд.
    Порог 0.15 с — вдвое ниже сигнала и много выше шума планировщика.
    """
    probe_delay = 0.2

    def slow(value):
        async def probe(_backend):
            await asyncio.sleep(probe_delay)
            return value

        return probe

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(capabilities, "aprobe_has_find", slow(True))
    monkeypatched.setattr(capabilities, "aprobe_has_grep", slow(False))
    try:
        backend = LocalFsBackend()
        gaps: list[float] = []
        stop = asyncio.Event()

        async def pulse():
            prev = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - prev)
                prev = now

        from gc_testkit import quiet_gc_for_loop_timing

        # Сборщик глушится на окне замера: пауза поколения 2 на куче полного
        # набора (180–220 мс) больше порога, и тест судил бы её, а не прогрев.
        with quiet_gc_for_loop_timing():
            beat = asyncio.create_task(pulse())
            while not gaps:
                await asyncio.sleep(0.005)
            gaps.clear()

            started = time.perf_counter()
            await backend.awarm_capabilities()
            elapsed = time.perf_counter() - started
            stop.set()
            await beat

        assert backend._has_grep_cache is not None
        assert elapsed >= probe_delay, (
            f"проверка пустая: прогрев занял {elapsed:.3f} с, то есть подменённые пробы не звались"
        )
        assert gaps, "пульс не тикал — замер пуст"
        assert max(gaps) < 0.15, (
            f"loop стоял {max(gaps) * 1000:.0f} мс при {probe_delay * 1000:.0f} мс "
            "ожидания в пробе — прогрев держит поток вместо того, чтобы ждать"
        )
    finally:
        monkeypatched.undo()


async def test_context_build_does_not_warm_the_probes(tmp_path, monkeypatch):
    """Сборка контекста пробы НЕ трогает — и это решение, а не забывчивость.

    Плату прогрев не убирает, а переносит: пробы кэшируются и стоят однократно
    на процесс, так что прогрев при сборке заставил бы платить за них КАЖДЫЙ
    прогон, включая те, что ни разу не ищут по файлам.

    Кому перенос нужен — зовёт ``awarm_capabilities()`` сам, вне горячего
    пути; ``fs_find_in_files`` именно так и делает, первым же шагом.
    """
    from gc_core.run_context import acreate_run_context
    from parity_harness import parity_config

    cfg = parity_config(tmp_path, monkeypatch)
    backend = LocalFsBackend()

    ctx = await acreate_run_context(cfg, os_backend=backend)

    assert ctx.os_backend is backend
    assert backend._has_grep_cache is None, (
        "сборка контекста прогрела пробы — значит платит за них каждый прогон"
    )
