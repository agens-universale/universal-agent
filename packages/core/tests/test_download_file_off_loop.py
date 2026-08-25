"""Гейт: `download_file` не пишет тело на event loop.

Тул сидит в категории A — диспетчер зовёт его как ``await acall(...)`` ПРЯМО
НА LOOP, без потока (``tools/__init__.py``, ветка ``await acall``). Сеть внутри
асинхронная, а запись файла — обычный синхронный ``open().write()``, и ``body``
держит всё тело в памяти: до ``max_bytes``, по умолчанию 50 МиБ.

То есть тул, который вызвался асинхронно, сам же морозил loop приложения на
время записи. Синхронные тулы от этого защищены — их уводит в поток диспетчер;
у async-тула такой защиты нет, он обязан позаботиться о себе сам.
"""
import asyncio
import ast

import pytest

from gc_testkit.paths import REPO

_SRC = REPO / "packages/core/src/gc_core/tools/exec/download_file.py"


def test_acall_wraps_the_write_in_a_thread():
    """Главная проверка: в ``acall`` запись идёт через ``to_thread``.

    Структурная, и намеренно: поведенческая проверка поймала бы регрессию
    только на большом файле и на нагруженной машине, а эта краснеет на самой
    попытке вернуть прямой вызов.
    """
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    acall = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "acall"
    )

    direct = [
        n for n in ast.walk(acall)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_atomic_write"
    ]
    assert not direct, (
        "_atomic_write зовётся напрямую из acall — тело до 50 МиБ пишется на loop; "
        "нужен await asyncio.to_thread(self._atomic_write, …)"
    )

    threaded = [
        n for n in ast.walk(acall)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "to_thread"
        and any(
            isinstance(a, ast.Attribute) and a.attr == "_atomic_write" for a in n.args
        )
    ]
    assert threaded, "не нашёл asyncio.to_thread(self._atomic_write, …) в acall"


@pytest.mark.asyncio
async def test_thread_write_disturbs_the_loop_less_than_a_direct_one(tmp_path):
    """Дифференциально: та же запись на loop против записи в потоке.

    Абсолютный порог тут не годится — длительность записи зависит от диска, и
    на быстром SSD 32 МиБ уходят за десяток миллисекунд, в который просто не
    помещается достаточно тиков. Поэтому сравниваем ДВА замера одной и той же
    настоящей записи: разрыв пульса при прямом вызове и при вызове через поток.
    Тест самокалибрующийся — он остаётся осмысленным и на медленном диске.
    """
    from gc_core.tools.exec.download_file import DownloadFile

    tool = object.__new__(DownloadFile)
    body = b"x" * (32 * 1024 * 1024)

    async def worst_gap(run) -> float:
        gaps: list[float] = []
        stop = asyncio.Event()

        async def pulse() -> None:
            prev = asyncio.get_running_loop().time()
            while not stop.is_set():
                await asyncio.sleep(0.002)
                now = asyncio.get_running_loop().time()
                gaps.append(now - prev)
                prev = now

        beat = asyncio.create_task(pulse())
        await asyncio.sleep(0.01)
        await run()
        stop.set()
        await beat
        return max(gaps) if gaps else float("inf")

    dest_direct = str(tmp_path / "direct.bin")
    dest_thread = str(tmp_path / "thread.bin")

    async def direct() -> None:
        tool._atomic_write(dest_direct, body)          # как было: на loop

    async def threaded() -> None:
        await asyncio.to_thread(tool._atomic_write, dest_thread, body)

    gap_direct = await worst_gap(direct)
    gap_thread = await worst_gap(threaded)

    assert (tmp_path / "thread.bin").stat().st_size == len(body)
    assert gap_thread < gap_direct, (
        f"запись в потоке мешает пульсу не меньше прямой: "
        f"{gap_thread * 1000:.1f}мс против {gap_direct * 1000:.1f}мс"
    )
