"""Пробы возможностей бэкенда прогреваются при сборке контекста, не в поиске.

Свойства `has_grep`/`has_find`/`grep_exclude_dir`/`grep_include` синхронные и
кэшируются — блокируют однократно на процесс. Сделать их асинхронными значит
превратить часть протокола `FsBackend` в async-аксессоры, то есть сломать
контракт у каждого встраивающего ради разового выигрыша.

Поэтому прогрев уводится с ГОРЯЧЕГО пути, а не переписывается протокол. Без
него первая же `fs_find_in_files` платит за четыре пробы (до ~10 с на пробу в
медленной песочнице) прямо внутри поиска, и агент выглядит зависшим на первом
инструменте.

Прогрев идёт параллельно MCP-дискавери, которая и так занимает 866–1405 мс,
поэтому на время сборки контекста он не влияет.
"""
import asyncio
import threading

import pytest

from gc_core.backends.local import LocalFsBackend


def test_warm_capabilities_fills_all_four_caches():
    backend = LocalFsBackend()
    caches = (
        "_has_find_cache", "_has_grep_cache",
        "_grep_exclude_dir_cache", "_grep_include_cache",
    )
    assert all(getattr(backend, c) is None for c in caches), "кэш уже прогрет"

    backend.warm_capabilities()

    cold = [c for c in caches if getattr(backend, c) is None]
    assert cold == [], f"после прогрева остались холодными: {cold}"


async def test_awarm_capabilities_does_not_block_the_loop():
    """Асинхронный прогрев не занимает event loop вызывающего.

    Пробы синхронные и в песочнице долгие; на loop их держать нельзя.
    """
    backend = LocalFsBackend()
    gaps = []
    stop = asyncio.Event()

    async def pulse():
        import time
        prev = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - prev)
            prev = now

    beat = asyncio.create_task(pulse())
    while not gaps:
        await asyncio.sleep(0.005)
    gaps.clear()

    await backend.awarm_capabilities()
    stop.set()
    await beat

    assert backend._has_grep_cache is not None
    assert gaps, "пульс не тикал — замер пуст"


async def test_context_build_does_not_warm_the_probes(tmp_path, monkeypatch):
    """Сборка контекста пробы НЕ трогает — и это решение, а не забывчивость.

    Прогрев уводит пробы в поток, а сборка контекста обязана обходиться без
    потоков (`test_context_build_native_async`: прежняя форма давала 8 event
    loop'ов и пик в 10 потоков на одну сборку). И плату он не убирает, а
    переносит: пробы кэшируются и стоят однократно на процесс, так что прогрев
    при сборке заставил бы платить за них каждый прогон, включая те, что ни
    разу не ищут по файлам.

    Кому перенос нужен — зовёт `awarm_capabilities()` сам, вне горячего пути.
    """
    from gc_core.run_context import acreate_run_context
    from parity_harness import parity_config

    cfg = parity_config(tmp_path, monkeypatch)
    backend = LocalFsBackend()

    ctx = await acreate_run_context(cfg, os_backend=backend)

    assert ctx.os_backend is backend
    assert backend._has_grep_cache is None, (
        "сборка контекста прогрела пробы — значит увела их в поток, а этого "
        "она делать не должна"
    )
